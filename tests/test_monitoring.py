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
            interval=1, gpu_type="none", start_epoch=time.time(),
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
            lambda *a, **_kw: MagicMock(returncode=0),
        )
        assert qp.detect_gpu() == "nvidia"

    def test_nvidia_timeout(self, monkeypatch):
        monkeypatch.setattr(
            shutil, "which",
            lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None,
        )
        def mock_run(*a, **_kw):
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
        def mock_run(cmd, **_kwargs):
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
        def mock_run(cmd, **_kwargs):
            if cmd[0] == "nvidia-smi":
                return MagicMock(stdout=csv_output, returncode=0)
            return MagicMock(stdout="0", returncode=0)
        monkeypatch.setattr(subprocess, "run", mock_run)
        result = qp.get_resource_snapshot("nvidia")
        assert "GPU" not in result


class TestDetectGpuMoved:
    """Tests for detect_gpu (moved from test_detection.py)."""

    def test_no_gpu_tools(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert qp.detect_gpu() == "none"

    def test_nvidia_smi_failure(self, monkeypatch):
        monkeypatch.setattr(
            shutil, "which",
            lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None,
        )
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **_kw: (_ for _ in ()).throw(
                subprocess.CalledProcessError(1, "nvidia-smi")
            ),
        )
        assert qp.detect_gpu() == "none"

    def test_rocm_smi_found(self, monkeypatch):
        monkeypatch.setattr(
            shutil, "which",
            lambda name: "/usr/bin/rocm-smi" if name == "rocm-smi" else None,
        )
        assert qp.detect_gpu() == "rocm"


class TestResourceMonitorInterval:
    """Test that the monitor thread ticks at least once."""

    def test_monitor_ticks_at_least_once(self, monkeypatch):
        """With a short interval, the monitor should produce at least one snapshot."""
        snapshots = []

        def capture_snapshot(gpu_type="none"):
            result = "CPU: mock | Mem: mock"
            snapshots.append(result)
            return result

        monkeypatch.setattr(qp.monitoring, "get_resource_snapshot", capture_snapshot)
        monitor = qp.ResourceMonitor(
            interval=0.1, gpu_type="none", start_epoch=time.time(),
        )
        monitor.start()
        time.sleep(0.3)
        monitor.stop()
        assert len(snapshots) >= 1

    def test_stop_is_idempotent(self, monkeypatch):
        """Calling stop() multiple times should not raise."""
        monkeypatch.setattr(
            qp.monitoring, "get_resource_snapshot", lambda gpu_type="none": "ok"
        )
        monitor = qp.ResourceMonitor(
            interval=1, gpu_type="none", start_epoch=time.time(),
        )
        monitor.start()
        monitor.stop()
        monitor.stop()  # should not raise


class TestGetGpuInfo:
    """Test GPU info helpers with edge cases."""

    def test_nvidia_unexpected_output(self, monkeypatch):
        """nvidia-smi returning garbage should not crash."""
        def mock_run(cmd, **_kwargs):
            if cmd[0] == "nvidia-smi":
                return MagicMock(stdout="unexpected output format\n", returncode=0)
            return MagicMock(stdout="0", returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = qp.get_resource_snapshot("nvidia")
        # Should not crash, GPU info may or may not appear
        assert isinstance(result, str)

    def test_nvidia_smi_timeout_in_snapshot(self, monkeypatch):
        """nvidia-smi timing out during snapshot should be handled."""
        def mock_run(cmd, **_kwargs):
            if cmd[0] == "nvidia-smi":
                raise subprocess.TimeoutExpired("nvidia-smi", 5)
            return MagicMock(stdout="0", returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = qp.get_resource_snapshot("nvidia")
        assert isinstance(result, str)
        assert "GPU" not in result

    def test_rocm_smi_with_utilization(self, monkeypatch):
        """rocm-smi returning utilization should be included."""
        def mock_run(cmd, **_kwargs):
            if cmd[0] == "rocm-smi":
                return MagicMock(
                    stdout="GPU[0] : GPU use: 75%\n", returncode=0,
                )
            return MagicMock(stdout="0", returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = qp.get_resource_snapshot("rocm")
        assert "75%" in result

    def test_rocm_smi_zero_utilization(self, monkeypatch):
        """rocm-smi returning 0% should be omitted."""
        def mock_run(cmd, **_kwargs):
            if cmd[0] == "rocm-smi":
                return MagicMock(
                    stdout="GPU[0] : GPU use: 0%\n", returncode=0,
                )
            return MagicMock(stdout="0", returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = qp.get_resource_snapshot("rocm")
        assert "GPU" not in result
