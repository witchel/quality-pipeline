"""Tests for quality_pipeline.output — ColorOutput, format_duration, gate_label."""

from __future__ import annotations

import io
import sys

import quality_pipeline as qp


class TestFormatDuration:
    def test_seconds_only(self):
        assert qp.format_duration(42) == "42s"

    def test_zero(self):
        assert qp.format_duration(0) == "0s"

    def test_exact_minute(self):
        assert qp.format_duration(60) == "1m 0s"

    def test_minutes_and_seconds(self):
        assert qp.format_duration(125) == "2m 5s"

    def test_exact_hour(self):
        assert qp.format_duration(3600) == "1h 0m 0s"

    def test_hours_minutes_seconds(self):
        assert qp.format_duration(3661) == "1h 1m 1s"

    def test_boundary_59(self):
        assert qp.format_duration(59) == "59s"

    def test_boundary_3599(self):
        assert qp.format_duration(3599) == "59m 59s"


class TestGateLabel:
    def test_hard(self):
        result = qp.gate_label("hard")
        assert "HARD" in result

    def test_soft(self):
        result = qp.gate_label("soft")
        assert "SOFT" in result

    def test_none(self):
        result = qp.gate_label("none")
        assert "NONE" in result

    def test_unknown_passthrough(self):
        assert qp.gate_label("custom") == "custom"


class TestColorOutput:
    def test_creates_instance(self):
        c = qp.ColorOutput()
        assert hasattr(c, "RED")
        assert hasattr(c, "NC")
        assert hasattr(c, "GREEN")
        assert hasattr(c, "YELLOW")
        assert hasattr(c, "BLUE")
        assert hasattr(c, "BOLD")

    def test_no_tty_no_colors(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        c = qp.ColorOutput()
        assert c.RED == ""
        assert c.GREEN == ""
        assert c.YELLOW == ""
        assert c.BLUE == ""
        assert c.BOLD == ""
        assert c.NC == ""

    def test_log_writes_to_stdout(self, capsys):
        c = qp.ColorOutput()
        c.log("hello")
        captured = capsys.readouterr()
        assert "hello" in captured.out
        assert "[pipeline]" in captured.out

    def test_err_writes_to_stderr(self, capsys):
        c = qp.ColorOutput()
        c.err("oops")
        captured = capsys.readouterr()
        assert "oops" in captured.err
