"""Tests for quality_pipeline.cleanup — PipelineCleanup, signal handling."""

from __future__ import annotations

import atexit
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import quality_pipeline as qp


class TestPipelineCleanup:
    def test_make_temp_creates_file(self):
        cleanup = qp.PipelineCleanup()
        p = cleanup.make_temp()
        assert p.exists()
        assert p in cleanup.temp_files
        # Manual cleanup
        p.unlink()

    def test_cleanup_removes_temp_files(self):
        cleanup = qp.PipelineCleanup()
        p = cleanup.make_temp()
        assert p.exists()
        cleanup.current_round = ""  # suppress interrupt message
        cleanup.cleanup()
        assert not p.exists()

    def test_cleanup_removes_lock_dir(self, tmp_path):
        cleanup = qp.PipelineCleanup()
        lock = tmp_path / "test.lock"
        lock.mkdir()
        cleanup.lock_dir = lock
        cleanup.current_round = ""
        cleanup.cleanup()
        assert not lock.exists()

    def test_cleanup_stops_monitor(self):
        cleanup = qp.PipelineCleanup()
        monitor = MagicMock()
        cleanup.monitor = monitor
        cleanup.cleanup()
        monitor.stop.assert_called_once()

    def test_cleanup_prints_interrupt_message(self, capsys):
        cleanup = qp.PipelineCleanup()
        cleanup.current_round = "audit-tests"
        cleanup.worktree_mode = False
        cleanup.cleanup()
        captured = capsys.readouterr()
        assert "audit-tests" in captured.err or "audit-tests" in captured.out

    def test_cleanup_idempotent(self, capsys):
        """Double cleanup should only print the interrupt message once."""
        cleanup = qp.PipelineCleanup()
        cleanup.current_round = "audit-tests"
        cleanup.worktree_mode = False
        cleanup.cleanup()
        first = capsys.readouterr()
        cleanup.cleanup()
        second = capsys.readouterr()
        assert "audit-tests" in first.err or "audit-tests" in first.out
        assert "audit-tests" not in second.err and "audit-tests" not in second.out

    def test_cleanup_worktree_mode_message(self, capsys):
        cleanup = qp.PipelineCleanup()
        cleanup.current_round = "audit-tests"
        cleanup.worktree_mode = True
        cleanup.cleanup()
        captured = capsys.readouterr()
        combined = captured.err + captured.out
        assert "unchanged" in combined.lower() or "worktree" in combined.lower()


class TestActivate:
    def test_registers_atexit_and_signals(self, monkeypatch):
        cleanup = qp.PipelineCleanup()
        registered = []
        monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn))
        signals_set = []
        orig_signal = signal.signal
        def mock_signal(sig, handler):
            signals_set.append(sig)
            return orig_signal(sig, handler)
        monkeypatch.setattr(signal, "signal", mock_signal)
        cleanup.activate()
        assert cleanup.cleanup in registered
        assert signal.SIGINT in signals_set
        assert signal.SIGTERM in signals_set

    def test_idempotent(self, monkeypatch):
        cleanup = qp.PipelineCleanup()
        call_count = [0]
        monkeypatch.setattr(atexit, "register", lambda fn: call_count.__setitem__(0, call_count[0] + 1))
        cleanup.activate()
        cleanup.activate()
        assert call_count[0] == 1

    def test_catches_valueerror_from_non_main_thread(self, monkeypatch):
        cleanup = qp.PipelineCleanup()
        monkeypatch.setattr(atexit, "register", lambda fn: None)
        monkeypatch.setattr(signal, "signal", MagicMock(side_effect=ValueError("not main thread")))
        cleanup.activate()  # should not raise
        assert cleanup._activated


class TestCleanupWorktree:
    def test_restores_cwd_and_removes_worktree(self, tmp_path, monkeypatch):
        cleanup = qp.PipelineCleanup()
        orig = tmp_path / "orig"
        orig.mkdir()
        wt = tmp_path / "worktree"
        wt.mkdir()
        cleanup.worktree_dir = wt
        cleanup.original_dir = orig
        cleanup.symlink_dirs = []

        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        cleanup._cleanup_worktree()
        assert Path.cwd() == orig
        assert cleanup.worktree_dir is None

    def test_chdir_failure_continues_cleanup(self, tmp_path, monkeypatch):
        """If original_dir is gone, cleanup should still remove worktree."""
        cleanup = qp.PipelineCleanup()
        gone = tmp_path / "gone"
        wt = tmp_path / "worktree"
        wt.mkdir()
        cleanup.worktree_dir = wt
        cleanup.original_dir = gone  # does not exist
        cleanup.symlink_dirs = []

        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        cleanup._cleanup_worktree()  # should not raise
        assert cleanup.worktree_dir is None

    def test_removes_symlinks(self, tmp_path, monkeypatch):
        cleanup = qp.PipelineCleanup()
        orig = tmp_path / "orig"
        orig.mkdir()
        wt = tmp_path / "worktree"
        wt.mkdir()

        # Create a symlink in worktree
        target = orig / "node_modules"
        target.mkdir()
        link = wt / "node_modules"
        link.symlink_to(target)

        cleanup.worktree_dir = wt
        cleanup.original_dir = orig
        cleanup.symlink_dirs = ["node_modules"]

        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        cleanup._cleanup_worktree()
        assert not link.exists()

    def test_fallback_on_git_worktree_remove_failure(self, tmp_path, monkeypatch):
        cleanup = qp.PipelineCleanup()
        orig = tmp_path / "orig"
        orig.mkdir()
        wt = tmp_path / "worktree"
        wt.mkdir()
        (wt / "somefile.txt").write_text("data")

        cleanup.worktree_dir = wt
        cleanup.original_dir = orig
        cleanup.symlink_dirs = []

        call_count = [0]
        def mock_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise subprocess.CalledProcessError(1, cmd)
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        cleanup._cleanup_worktree()
        # Worktree should be removed by shutil.rmtree fallback
        assert not wt.exists()


class TestHandleSignal:
    def test_exits_with_128_plus_signum(self):
        with pytest.raises(SystemExit) as exc_info:
            qp._handle_signal(15, None)
        assert exc_info.value.code == 128 + 15

    def test_sigint_code(self):
        with pytest.raises(SystemExit) as exc_info:
            qp._handle_signal(2, None)
        assert exc_info.value.code == 130
