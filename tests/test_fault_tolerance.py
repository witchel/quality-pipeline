"""Fault-tolerance tests: atomic writes, fsync, stale lock detection, idempotency."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import quality_pipeline as qp
from tests.conftest import _mock_git_fn


# ---------------------------------------------------------------------------
# A. atomic_write_text
# ---------------------------------------------------------------------------


class TestAtomicWriteText:
    def test_writes_content_atomically(self, tmp_path):
        target = tmp_path / "state.json"
        qp.atomic_write_text(target, '{"key": "value"}')
        assert target.read_text() == '{"key": "value"}'

    def test_preserves_original_on_write_failure(self, tmp_path):
        """If the write fails, the original file must remain intact."""
        target = tmp_path / "state.json"
        target.write_text("original")

        with patch("quality_pipeline.output.os.fsync", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                qp.atomic_write_text(target, "new content")

        assert target.read_text() == "original"

    def test_cleans_up_temp_file_on_error(self, tmp_path):
        """Temp file must not linger after a failed write."""
        target = tmp_path / "state.json"
        before = set(tmp_path.iterdir())

        with patch("quality_pipeline.output.os.fsync", side_effect=OSError("fail")):
            with pytest.raises(OSError):
                qp.atomic_write_text(target, "content")

        after = set(tmp_path.iterdir())
        assert after == before, f"Temp files left behind: {after - before}"

    def test_overwrites_existing_file(self, tmp_path):
        target = tmp_path / "config.yaml"
        target.write_text("old")
        qp.atomic_write_text(target, "new")
        assert target.read_text() == "new"

    def test_creates_new_file(self, tmp_path):
        target = tmp_path / "brand-new.txt"
        assert not target.exists()
        qp.atomic_write_text(target, "hello")
        assert target.read_text() == "hello"

    def test_temp_file_in_same_directory(self, tmp_path):
        """Temp file must be in the same directory for atomic rename."""
        target = tmp_path / "data.txt"
        created_temps = []
        original_mkstemp = qp.output.tempfile.mkstemp

        def tracking_mkstemp(**kwargs):
            result = original_mkstemp(**kwargs)
            created_temps.append((kwargs.get("dir"), result[1]))
            return result

        with patch.object(qp.output.tempfile, "mkstemp", side_effect=tracking_mkstemp):
            qp.atomic_write_text(target, "content")

        assert len(created_temps) == 1
        temp_dir = created_temps[0][0]
        assert Path(temp_dir) == tmp_path


class TestFsyncDirectory:
    def test_calls_fsync_on_directory(self, tmp_path):
        with patch("quality_pipeline.output.os.fsync") as mock_fsync, \
             patch("quality_pipeline.output.os.open", return_value=42) as mock_open, \
             patch("quality_pipeline.output.os.close") as mock_close:
            qp._fsync_directory(tmp_path)
            mock_open.assert_called_once_with(str(tmp_path), os.O_RDONLY)
            mock_fsync.assert_called_once_with(42)
            mock_close.assert_called_once_with(42)

    def test_handles_osError_gracefully(self, tmp_path):
        """fsync on directory is best-effort; OSError should be swallowed."""
        with patch("quality_pipeline.output.os.open", side_effect=OSError("not supported")):
            qp._fsync_directory(tmp_path)  # Should not raise


class TestAtomicWriteFsyncOrder:
    """Verify fsync is called at the correct points in the atomic write."""

    def test_fsync_before_rename(self, tmp_path):
        """os.fsync must be called on the fd before os.replace."""
        target = tmp_path / "f.txt"
        call_order = []

        orig_fsync = os.fsync
        orig_replace = os.replace

        def tracking_fsync(fd):
            call_order.append("fsync")
            return orig_fsync(fd)

        def tracking_replace(src, dst):
            call_order.append("replace")
            return orig_replace(src, dst)

        with patch("quality_pipeline.output.os.fsync", side_effect=tracking_fsync), \
             patch("quality_pipeline.output.os.replace", side_effect=tracking_replace):
            qp.atomic_write_text(target, "data")

        # First fsync is on the file fd (before rename)
        # Second fsync is the directory fsync (after rename)
        assert call_order[0] == "fsync"
        assert call_order[1] == "replace"

    def test_dir_fsync_after_rename(self, tmp_path):
        target = tmp_path / "f.txt"
        call_order = []

        orig_fsync = os.fsync
        orig_replace = os.replace

        def tracking_fsync(fd):
            call_order.append(("fsync", fd))
            return orig_fsync(fd)

        def tracking_replace(src, dst):
            call_order.append(("replace",))
            return orig_replace(src, dst)

        with patch("quality_pipeline.output.os.fsync", side_effect=tracking_fsync), \
             patch("quality_pipeline.output.os.replace", side_effect=tracking_replace):
            qp.atomic_write_text(target, "data")

        # Should have: file fsync, replace, dir fsync
        assert len(call_order) == 3
        assert call_order[0][0] == "fsync"   # file fsync
        assert call_order[1][0] == "replace"
        assert call_order[2][0] == "fsync"   # dir fsync


# ---------------------------------------------------------------------------
# B. Streaming write fsync (process.py)
# ---------------------------------------------------------------------------


class TestStreamingWriteFsync:
    """Verify that streaming output files are fsynced before close."""

    def test_run_tests_with_tee_fsyncs(self, tmp_path, monkeypatch):
        output_file = tmp_path / "test-output.txt"
        fsync_calls = []
        orig_fsync = os.fsync

        def tracking_fsync(fd):
            fsync_calls.append(fd)
            return orig_fsync(fd)

        monkeypatch.setattr("quality_pipeline.process.os.fsync", tracking_fsync)

        # Use a simple command that produces output
        exit_code = qp.run_tests_with_tee("echo hello", output_file)
        assert exit_code == 0
        assert len(fsync_calls) >= 1, "os.fsync was not called on test output"

    def test_run_claude_process_fsyncs(self, tmp_path, monkeypatch):
        output_file = tmp_path / "claude-output.json"
        fsync_calls = []
        orig_fsync = os.fsync

        def tracking_fsync(fd):
            fsync_calls.append(fd)
            return orig_fsync(fd)

        monkeypatch.setattr("quality_pipeline.process.os.fsync", tracking_fsync)

        # Run a simple command via _run_claude_process
        exit_code, timed_out = qp._run_claude_process(
            ["echo", "result"], output_file
        )
        assert exit_code == 0
        assert not timed_out
        assert len(fsync_calls) >= 1, "os.fsync was not called on claude output"


# ---------------------------------------------------------------------------
# C. Stale lock detection
# ---------------------------------------------------------------------------


class TestIsLockStale:
    def test_no_pid_file_returns_false(self, tmp_path):
        """Missing PID file → can't determine, conservative = not stale."""
        lock = tmp_path / "quality-pipeline.lock"
        lock.mkdir()
        assert qp._is_lock_stale(lock) is False

    def test_invalid_pid_file_returns_false(self, tmp_path):
        lock = tmp_path / "quality-pipeline.lock"
        lock.mkdir()
        pid_file = tmp_path / "quality-pipeline.lock.pid"
        pid_file.write_text("not-a-number")
        assert qp._is_lock_stale(lock) is False

    def test_dead_pid_returns_true(self, tmp_path):
        lock = tmp_path / "quality-pipeline.lock"
        lock.mkdir()
        pid_file = tmp_path / "quality-pipeline.lock.pid"
        # Use PID 0 isn't valid, use a very high PID that's almost certainly dead
        # Actually, we'll mock os.kill to raise ProcessLookupError
        pid_file.write_text("99999999")

        with patch("quality_pipeline.git_ops.os.kill", side_effect=ProcessLookupError):
            assert qp._is_lock_stale(lock) is True

    def test_alive_pid_returns_false(self, tmp_path):
        lock = tmp_path / "quality-pipeline.lock"
        lock.mkdir()
        pid_file = tmp_path / "quality-pipeline.lock.pid"
        pid_file.write_text(str(os.getpid()))  # This process is alive
        assert qp._is_lock_stale(lock) is False

    def test_permission_error_returns_false(self, tmp_path):
        """If we can't signal the PID (different user), assume live."""
        lock = tmp_path / "quality-pipeline.lock"
        lock.mkdir()
        pid_file = tmp_path / "quality-pipeline.lock.pid"
        pid_file.write_text("12345")

        with patch("quality_pipeline.git_ops.os.kill", side_effect=PermissionError):
            assert qp._is_lock_stale(lock) is False


class TestLockPidPath:
    def test_returns_sibling_pid_file(self):
        lock = Path("/some/dir/quality-pipeline.lock")
        result = qp._lock_pid_path(lock)
        assert result == Path("/some/dir/quality-pipeline.lock.pid")


class TestGitAcquireLockStaleness:
    def test_reclaims_stale_lock(self, tmp_path, monkeypatch):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock_path = git_dir / "quality-pipeline.lock"
        lock_path.mkdir()
        pid_file = git_dir / "quality-pipeline.lock.pid"
        pid_file.write_text("99999999")

        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(stdout=str(git_dir) + "\n"))

        def _kill_raises(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(qp.git_ops.os, "kill", _kill_raises)

        result = qp.git_acquire_lock(False)
        assert result == lock_path
        assert lock_path.is_dir()
        # PID file should be updated with our PID
        assert pid_file.read_text() == str(os.getpid())
        # Cleanup
        pid_file.unlink()
        lock_path.rmdir()

    def test_writes_pid_file_on_acquire(self, tmp_path, monkeypatch):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(stdout=str(git_dir) + "\n"))

        result = qp.git_acquire_lock(False)
        assert result is not None
        pid_file = git_dir / "quality-pipeline.lock.pid"
        assert pid_file.exists()
        assert pid_file.read_text() == str(os.getpid())
        # Cleanup
        pid_file.unlink()
        result.rmdir()

    def test_lock_blocks_if_pid_alive(self, tmp_path, monkeypatch):
        """If the PID in the lock file is alive, acquisition must fail."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock_path = git_dir / "quality-pipeline.lock"
        lock_path.mkdir()
        pid_file = git_dir / "quality-pipeline.lock.pid"
        pid_file.write_text(str(os.getpid()))  # Our own PID is alive

        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(stdout=str(git_dir) + "\n"))

        with pytest.raises(SystemExit):
            qp.git_acquire_lock(False)


# ---------------------------------------------------------------------------
# D. Cleanup with PID file
# ---------------------------------------------------------------------------


class TestCleanupWithPidFile:
    def test_cleanup_removes_pid_file(self, tmp_path):
        """Cleanup must remove both the lock directory and sibling PID file."""
        lock_dir = tmp_path / "quality-pipeline.lock"
        lock_dir.mkdir()
        pid_file = tmp_path / "quality-pipeline.lock.pid"
        pid_file.write_text(str(os.getpid()))

        cleanup = qp.PipelineCleanup()
        cleanup.lock_dir = lock_dir
        cleanup.cleanup()

        assert not lock_dir.exists()
        assert not pid_file.exists()

    def test_cleanup_handles_missing_pid_file(self, tmp_path):
        """Cleanup should not fail if PID file doesn't exist."""
        lock_dir = tmp_path / "quality-pipeline.lock"
        lock_dir.mkdir()

        cleanup = qp.PipelineCleanup()
        cleanup.lock_dir = lock_dir
        cleanup.cleanup()

        assert not lock_dir.exists()


# ---------------------------------------------------------------------------
# E. Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_atomic_write_twice_produces_same_result(self, tmp_path):
        """Writing the same content twice must be idempotent."""
        target = tmp_path / "state.json"
        content = '{"version": 1}'
        qp.atomic_write_text(target, content)
        first_content = target.read_text()
        qp.atomic_write_text(target, content)
        second_content = target.read_text()
        assert first_content == second_content == content

    def test_lock_acquire_release_acquire(self, tmp_path, monkeypatch):
        """After cleanup, a lock can be re-acquired (idempotent cycle)."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(stdout=str(git_dir) + "\n"))

        # First acquire
        result1 = qp.git_acquire_lock(False)
        assert result1 is not None

        # Simulate cleanup
        cleanup = qp.PipelineCleanup()
        cleanup.lock_dir = result1
        cleanup.cleanup()

        # Second acquire should succeed
        result2 = qp.git_acquire_lock(False)
        assert result2 is not None
        assert result2.is_dir()

        # Cleanup
        cleanup2 = qp.PipelineCleanup()
        cleanup2.lock_dir = result2
        cleanup2.cleanup()
