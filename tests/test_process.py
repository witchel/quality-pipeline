"""Tests for quality_pipeline.process — test runner, claude, reviewer."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import quality_pipeline as qp
from conftest import _mock_git_fn


class TestRunTestsWithTee:
    def test_success_captures_output(self, tmp_path):
        output_file = tmp_path / "output.txt"
        exit_code = qp.run_tests_with_tee("echo 'hello world'", output_file)
        assert exit_code == 0
        assert "hello world" in output_file.read_text()

    def test_failure_returns_nonzero(self, tmp_path):
        output_file = tmp_path / "output.txt"
        exit_code = qp.run_tests_with_tee("exit 1", output_file)
        assert exit_code == 1

    def test_stderr_merged_into_output(self, tmp_path):
        output_file = tmp_path / "output.txt"
        exit_code = qp.run_tests_with_tee("echo err >&2", output_file)
        assert exit_code == 0
        assert "err" in output_file.read_text()


class TestRunTestsTimeout:
    def test_timeout_returns_negative_one(self, tmp_path):
        output_file = tmp_path / "test.out"
        # sleep 60 will be killed quickly by the 1s timeout
        code = qp.run_tests_with_tee("sleep 60", output_file, timeout_seconds=1)
        assert code == -1

    def test_no_timeout_by_default(self, tmp_path):
        output_file = tmp_path / "test.out"
        code = qp.run_tests_with_tee("echo ok", output_file)
        assert code == 0


class TestRunClaude:
    @staticmethod
    def _mock_popen(returncode=0):
        def factory(*args, **kwargs):
            proc = MagicMock()
            proc.stdout = io.StringIO("")
            proc.stderr = io.StringIO("")
            proc.returncode = returncode
            proc.wait.return_value = returncode
            proc.kill.return_value = None
            factory.last_cmd = args[0] if args else kwargs.get("args")
            return proc
        factory.last_cmd = None
        return factory

    def test_returns_exit_code(self, tmp_path, monkeypatch):
        log_file = tmp_path / "claude.log"
        monkeypatch.setattr(subprocess, "Popen", self._mock_popen(0))
        code = qp.run_claude("prompt", "ctx", 5.0, 20, log_file)
        assert code == 0

    def test_returns_nonzero(self, tmp_path, monkeypatch):
        log_file = tmp_path / "claude.log"
        monkeypatch.setattr(subprocess, "Popen", self._mock_popen(1))
        code = qp.run_claude("prompt", "ctx", 5.0, 20, log_file)
        assert code == 1

    def test_passes_budget_and_turns(self, tmp_path, monkeypatch):
        log_file = tmp_path / "claude.log"
        mock = self._mock_popen(0)
        monkeypatch.setattr(subprocess, "Popen", mock)
        qp.run_claude("prompt", "ctx", 3.50, 10, log_file)
        assert "3.50" in mock.last_cmd
        assert "10" in mock.last_cmd

    def test_timeout_returns_negative_one(self, tmp_path, monkeypatch):
        log_file = tmp_path / "claude.log"
        import threading as _threading

        class InstantTimer:
            """Timer that calls its callback immediately on start()."""
            def __init__(self, interval, function):
                self._fn = function
            def start(self):
                self._fn()
            def cancel(self):
                pass

        monkeypatch.setattr(_threading, "Timer", InstantTimer)
        monkeypatch.setattr(subprocess, "Popen", self._mock_popen(-9))
        code = qp.run_claude(
            "prompt", "ctx", 5.0, 20, log_file, timeout_minutes=1,
        )
        assert code == -1


class TestRunReviewer:
    def _make_rc(self, review=True):
        return qp.RoundConfig(name="test", review=review)

    def test_skips_when_review_disabled(self, monkeypatch):
        """Should return immediately when review is not enabled."""
        calls = []
        monkeypatch.setattr(qp.process, "git", lambda *a, **kw: calls.append(a))
        qp.run_reviewer(1, self._make_rc(review=False), "abc", Path("/tmp"), None)
        assert len(calls) == 0  # no git calls made

    def test_cli_flag_overrides_rc(self, monkeypatch):
        """review_flag=False should override rc.review=True."""
        calls = []
        monkeypatch.setattr(qp.process, "git", lambda *a, **kw: calls.append(a))
        qp.run_reviewer(1, self._make_rc(review=True), "abc", Path("/tmp"), False)
        assert len(calls) == 0

    def test_cli_flag_enables_review(self, tmp_path, monkeypatch):
        """review_flag=True should enable review even when rc.review=False."""
        monkeypatch.setattr(
            qp.process, "git",
            _mock_git_fn(stdout="diff content here\n"),
        )
        monkeypatch.setattr(qp.process, "TEMPLATE_DIR", tmp_path)
        (tmp_path / "reviewer.md").write_text("Review: DIFF_PLACEHOLDER")
        def mock_popen(*args, **kwargs):
            proc = MagicMock()
            proc.stdout = io.StringIO('{"verdict": "pass"}')
            proc.stderr = io.StringIO("")
            proc.returncode = 0
            proc.wait.return_value = 0
            return proc

        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        qp.run_reviewer(
            1, self._make_rc(review=False), "abc", log_dir, True,
        )
        assert list(log_dir.glob("review-*.json"))

    def test_no_diff_skips_review(self, tmp_path, monkeypatch):
        """When diff is empty, reviewer should skip."""
        monkeypatch.setattr(qp.process, "git", _mock_git_fn(stdout=""))
        monkeypatch.setattr(qp.process, "TEMPLATE_DIR", tmp_path)
        (tmp_path / "reviewer.md").write_text("Review: DIFF_PLACEHOLDER")
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        qp.run_reviewer(1, self._make_rc(), "abc", log_dir, None)
        assert not list(log_dir.glob("review-*.json"))

    def test_missing_template_skips(self, tmp_path, monkeypatch):
        """When template file is missing, reviewer should skip."""
        monkeypatch.setattr(
            qp.process, "git", _mock_git_fn(stdout="some diff\n"),
        )
        monkeypatch.setattr(qp.process, "TEMPLATE_DIR", tmp_path / "nonexistent")
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        qp.run_reviewer(1, self._make_rc(), "abc", log_dir, None)
        assert not list(log_dir.glob("review-*.json"))

    def _run_reviewer_with_verdict(self, tmp_path, monkeypatch, verdict_json):
        """Helper: run reviewer and write a specific verdict to the output file."""
        monkeypatch.setattr(
            qp.process, "git", _mock_git_fn(stdout="diff content\n"),
        )
        monkeypatch.setattr(qp.process, "TEMPLATE_DIR", tmp_path)
        (tmp_path / "reviewer.md").write_text("Review: DIFF_PLACEHOLDER")

        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        def mock_popen(*args, **kwargs):
            proc = MagicMock()
            proc.stdout = io.StringIO(verdict_json)
            proc.stderr = io.StringIO("")
            proc.returncode = 0
            proc.wait.return_value = 0
            return proc

        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        qp.run_reviewer(1, self._make_rc(), "abc", log_dir, None)
        return log_dir

    def test_verdict_pass(self, tmp_path, monkeypatch, capsys):
        self._run_reviewer_with_verdict(
            tmp_path, monkeypatch, json.dumps({"verdict": "pass"})
        )
        out = capsys.readouterr().out
        assert "PASS" in out

    def test_verdict_warn(self, tmp_path, monkeypatch, capsys):
        self._run_reviewer_with_verdict(
            tmp_path, monkeypatch, json.dumps({"verdict": "warn"})
        )
        out = capsys.readouterr().out
        assert "WARN" in out

    def test_verdict_critical(self, tmp_path, monkeypatch, capsys):
        self._run_reviewer_with_verdict(
            tmp_path, monkeypatch, json.dumps({"verdict": "critical"})
        )
        captured = capsys.readouterr()
        assert "CRITICAL" in captured.err or "CRITICAL" in captured.out

    def test_verdict_unparseable(self, tmp_path, monkeypatch, capsys):
        self._run_reviewer_with_verdict(
            tmp_path, monkeypatch, "not json at all"
        )
        out = capsys.readouterr().out
        assert "could not parse" in out

    def test_verdict_wrapped_in_result(self, tmp_path, monkeypatch, capsys):
        """Handle claude JSON output wrapping verdict in a 'result' key."""
        inner = json.dumps({"verdict": "pass"})
        outer = json.dumps({"result": inner})
        self._run_reviewer_with_verdict(tmp_path, monkeypatch, outer)
        out = capsys.readouterr().out
        assert "PASS" in out

    def test_verdict_in_code_block(self, tmp_path, monkeypatch, capsys):
        """Handle verdict wrapped in markdown code fences."""
        verdict = '```json\n{"verdict": "warn"}\n```'
        self._run_reviewer_with_verdict(tmp_path, monkeypatch, verdict)
        out = capsys.readouterr().out
        assert "WARN" in out
