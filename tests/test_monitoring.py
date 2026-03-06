"""Tests for quality_pipeline.monitoring — ResourceMonitor, get_resource_snapshot."""

from __future__ import annotations

import shutil
import subprocess
import time
from unittest.mock import MagicMock

import quality_pipeline as qp


class TestResourceMonitor:
    def test_start_and_stop(self, monkeypatch):
        """Monitor should start and stop cleanly without errors."""
        monkeypatch.setattr(
            qp.monitoring, "get_resource_snapshot", lambda gpu_type="none": "CPU: ok"
        )
        monitor = qp.ResourceMonitor(
            interval=1, gpu_type="none", round_name="test", start_epoch=time.time()
        )
        monitor.start()
        assert monitor._thread.is_alive()
        monitor.stop()
        assert not monitor._thread.is_alive()


class TestDetectGpu:
    def test_nvidia_success(self, monkeypatch):
        monkeypatch.setattr(
            shutil, "which",
            lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None,
        )
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        assert qp.detect_gpu() == "nvidia"

    def test_nvidia_timeout(self, monkeypatch):
        monkeypatch.setattr(
            shutil, "which",
            lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None,
        )
        def mock_run(*a, **kw):
            raise subprocess.TimeoutExpired("nvidia-smi", 5)
        monkeypatch.setattr(subprocess, "run", mock_run)
        assert qp.detect_gpu() == "none"


class TestGetResourceSnapshot:
    def test_returns_string(self):
        result = qp.get_resource_snapshot()
        assert isinstance(result, str)
        assert "CPU:" in result
        assert "Mem:" in result

    def test_with_gpu_none(self):
        result = qp.get_resource_snapshot("none")
        assert "GPU" not in result

    def test_nvidia_gpu_snapshot(self, monkeypatch):
        """Mock nvidia-smi CSV output and verify GPU info is included."""
        csv_output = "0, 45, 2048, 8192\n1, 0, 100, 8192\n"
        def mock_run(cmd, **kwargs):
            if cmd[0] == "nvidia-smi":
                return MagicMock(stdout=csv_output, returncode=0)
            # Fall through for sysctl/vm_stat
            return MagicMock(stdout="0", returncode=0)
        monkeypatch.setattr(subprocess, "run", mock_run)
        result = qp.get_resource_snapshot("nvidia")
        # GPU0 has 45% util (active), so should appear
        assert "GPU0" in result
        assert "45%" in result

    def test_nvidia_gpu_all_idle(self, monkeypatch):
        """When all GPUs are idle, GPU info should be omitted."""
        csv_output = "0, 0, 100, 8192\n"
        def mock_run(cmd, **kwargs):
            if cmd[0] == "nvidia-smi":
                return MagicMock(stdout=csv_output, returncode=0)
            return MagicMock(stdout="0", returncode=0)
        monkeypatch.setattr(subprocess, "run", mock_run)
        result = qp.get_resource_snapshot("nvidia")
        assert "GPU" not in result
