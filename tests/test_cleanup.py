"""Tests for quality_pipeline.cleanup — PipelineCleanup, signal handling."""

from __future__ import annotations

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


class TestHandleSignal:
    def test_exits_with_128_plus_signum(self):
        with pytest.raises(SystemExit) as exc_info:
            qp._handle_signal(15, None)
        assert exc_info.value.code == 128 + 15

    def test_sigint_code(self):
        with pytest.raises(SystemExit) as exc_info:
            qp._handle_signal(2, None)
        assert exc_info.value.code == 130
