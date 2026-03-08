"""Fault-tolerance tests: atomic writes, fsync, stale lock detection, idempotency."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import quality_pipeline as qp
from conftest import _mock_git_fn


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

        def _kill_raises(_pid, _sig):
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


# ---------------------------------------------------------------------------
# F. Atomic PID file write
# ---------------------------------------------------------------------------


class TestPidFileAtomicWrite:
    """Verify that git_acquire_lock writes the PID file atomically."""

    def test_pid_file_uses_atomic_write(self, tmp_path, monkeypatch):
        """PID file must be written via atomic_write_text, not plain write_text."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(stdout=str(git_dir) + "\n"))

        atomic_calls = []
        orig_atomic = qp.output.atomic_write_text

        def tracking_atomic(path, content):
            atomic_calls.append((path, content))
            return orig_atomic(path, content)

        monkeypatch.setattr(qp.git_ops, "atomic_write_text", tracking_atomic)

        result = qp.git_acquire_lock(False)
        assert result is not None

        # Verify atomic_write_text was called for the PID file
        pid_file = git_dir / "quality-pipeline.lock.pid"
        pid_calls = [(p, c) for p, c in atomic_calls if p == pid_file]
        assert len(pid_calls) == 1, "PID file should be written atomically exactly once"
        assert pid_calls[0][1] == str(os.getpid())

        # Cleanup
        pid_file.unlink()
        result.rmdir()

    def test_pid_file_survives_simulated_crash(self, tmp_path, monkeypatch):
        """If atomic write fails, no corrupt PID file should exist."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(stdout=str(git_dir) + "\n"))

        # Pre-populate a valid PID file from a "previous run"
        pid_file = git_dir / "quality-pipeline.lock.pid"
        pid_file.write_text("12345")

        def failing_atomic(path, content):
            if path == pid_file:
                raise OSError("simulated disk failure")
            return qp.output.atomic_write_text(path, content)

        monkeypatch.setattr(qp.git_ops, "atomic_write_text", failing_atomic)

        # The lock mkdir succeeds, but PID write fails — should propagate
        with pytest.raises(OSError, match="simulated disk failure"):
            qp.git_acquire_lock(False)

        # The old PID file content should be intact (atomic_write_text
        # guarantees no partial writes)
        assert pid_file.read_text() == "12345"

        # Cleanup the lock dir that was created before the failure
        lock_path = git_dir / "quality-pipeline.lock"
        if lock_path.exists():
            lock_path.rmdir()


# ---------------------------------------------------------------------------
# G. Cleanup double-call idempotency
# ---------------------------------------------------------------------------


class TestCleanupDoubleCallIdempotency:
    """Verify that cleanup() can be called twice safely (signal + atexit)."""

    def test_monitor_stopped_only_once(self):
        """Monitor.stop() should only be called once across two cleanup() calls."""
        cleanup = qp.PipelineCleanup()
        monitor = MagicMock()
        cleanup.monitor = monitor

        cleanup.cleanup()
        assert monitor.stop.call_count == 1
        assert cleanup.monitor is None

        cleanup.cleanup()
        # Still only one call — second cleanup sees monitor=None
        assert monitor.stop.call_count == 1

    def test_temp_files_cleared_after_cleanup(self):
        """temp_files list should be empty after first cleanup call."""
        cleanup = qp.PipelineCleanup()
        p = cleanup.make_temp()
        assert p.exists()
        assert len(cleanup.temp_files) == 1

        cleanup.cleanup()
        assert not p.exists()
        assert cleanup.temp_files == []

        # Second cleanup is a no-op for temp files
        cleanup.cleanup()
        assert cleanup.temp_files == []

    def test_lock_dir_cleared_after_cleanup(self, tmp_path):
        """lock_dir should be None after first cleanup call."""
        lock_dir = tmp_path / "quality-pipeline.lock"
        lock_dir.mkdir()

        cleanup = qp.PipelineCleanup()
        cleanup.lock_dir = lock_dir

        cleanup.cleanup()
        assert not lock_dir.exists()
        assert cleanup.lock_dir is None

        # Second cleanup doesn't touch lock_dir
        cleanup.cleanup()
        assert cleanup.lock_dir is None

    def test_full_double_cleanup_no_errors(self, tmp_path):
        """Full cleanup with all resources should work twice without errors."""
        lock_dir = tmp_path / "quality-pipeline.lock"
        lock_dir.mkdir()
        pid_file = tmp_path / "quality-pipeline.lock.pid"
        pid_file.write_text("12345")

        cleanup = qp.PipelineCleanup()
        cleanup.lock_dir = lock_dir
        cleanup.monitor = MagicMock()
        p = cleanup.make_temp()
        cleanup.current_round = "test-round"

        # First cleanup: everything is processed
        cleanup.cleanup()
        assert cleanup.monitor is None
        assert cleanup.temp_files == []
        assert cleanup.lock_dir is None
        assert cleanup.current_round == ""
        assert not p.exists()
        assert not lock_dir.exists()

        # Second cleanup: no-op, no errors
        cleanup.cleanup()
