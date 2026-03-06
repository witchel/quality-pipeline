"""Tests for quality_pipeline.monitoring — ResourceMonitor, get_resource_snapshot."""

from __future__ import annotations

import time

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


class TestGetResourceSnapshot:
    def test_returns_string(self):
        result = qp.get_resource_snapshot()
        assert isinstance(result, str)
        assert "CPU:" in result
        assert "Mem:" in result

    def test_with_gpu_none(self):
        result = qp.get_resource_snapshot("none")
        assert "GPU" not in result
