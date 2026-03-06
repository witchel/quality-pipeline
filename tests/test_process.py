"""Tests for quality_pipeline.process — test runner, claude, reviewer."""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import quality_pipeline as qp
from conftest import _mock_git_fn


class TestKillProcessGroup:
    def test_skips_invalid_pid_zero(self):
        proc = MagicMock()
        proc.pid = 0
        with patch.object(os, "killpg") as mock_killpg:
            qp._kill_process_group(proc)
            mock_killpg.assert_not_called()

    def test_skips_invalid_pid_negative(self):
        proc = MagicMock()
        proc.pid = -1
        with patch.object(os, "killpg") as mock_killpg:
            qp._kill_process_group(proc)
            mock_killpg.assert_not_called()

    def test_skips_pid_none(self):
        proc = MagicMock()
        proc.pid = None
        with patch.object(os, "killpg") as mock_killpg:
            qp._kill_process_group(proc)
            mock_killpg.assert_not_called()

    def test_sends_sigterm(self):
        proc = MagicMock()
        proc.pid = 12345
        proc.wait.return_value = 0
        with patch.object(os, "killpg") as mock_killpg:
            qp._kill_process_group(proc)
            mock_killpg.assert_called_once_with(12345, signal.SIGTERM)

    def test_escalates_to_sigkill_on_timeout(self):
        proc = MagicMock()
        proc.pid = 12345
        proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 2)
        with patch.object(os, "killpg") as mock_killpg:
            qp._kill_process_group(proc, graceful_wait=0.01)
            calls = mock_killpg.call_args_list
            assert calls[0] == ((12345, signal.SIGTERM),)
            assert calls[1] == ((12345, signal.SIGKILL),)

    def test_handles_oserror_on_sigterm(self):
        proc = MagicMock()
        proc.pid = 12345
        with patch.object(os, "killpg", side_effect=OSError("no such process")):
            qp._kill_process_group(proc)  # should not raise

    def test_handles_oserror_on_sigkill(self):
        proc = MagicMock()
        proc.pid = 12345
        proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 2)
        effects = [None, OSError("no such process")]
        with patch.object(os, "killpg", side_effect=effects):
            qp._kill_process_group(proc, graceful_wait=0.01)  # should not raise


class TestClaudeEnv:
    def test_strips_claudecode_vars(self, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "/bin/claude")
        monkeypatch.setenv("HOME", "/home/test")
        env = qp._claude_env()
        assert "CLAUDECODE" not in env
        assert "CLAUDE_CODE_ENTRYPOINT" not in env
        assert env["HOME"] == "/home/test"

    def test_preserves_env_when_vars_absent(self, monkeypatch):
        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
        env = qp._claude_env()
        assert "CLAUDECODE" not in env


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


class TestRunTestsStdbuf:
    def test_stdbuf_prepended_when_available(self, tmp_path, monkeypatch):
        """When stdbuf is available, it should be prepended to the command."""
        output_file = tmp_path / "output.txt"
        captured_cmds = []
        original_popen = subprocess.Popen

        def capture_popen(cmd, **_kwargs):
            captured_cmds.append(cmd)
            # Replace the stdbuf-prefixed cmd with just the original command
            # so the test can actually run
            clean_cmd = cmd
            for prefix in ("stdbuf -oL ", "gstdbuf -oL "):
                if cmd.startswith(prefix):
                    clean_cmd = cmd[len(prefix):]
                    break
            return original_popen(clean_cmd, **_kwargs)

        monkeypatch.setattr(subprocess, "Popen", capture_popen)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/stdbuf" if name == "stdbuf" else None)
        exit_code = qp.run_tests_with_tee("echo stdbuf_test", output_file)
        assert exit_code == 0
        assert captured_cmds[0].startswith("stdbuf -oL ")

    def test_no_stdbuf_still_works(self, tmp_path, monkeypatch):
        """When neither stdbuf nor gstdbuf is available, command runs normally."""
        output_file = tmp_path / "output.txt"
        monkeypatch.setattr(shutil, "which", lambda name: None)
        exit_code = qp.run_tests_with_tee("echo no_stdbuf", output_file)
        assert exit_code == 0
        assert "no_stdbuf" in output_file.read_text()


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
            proc.pid = -1  # invalid PID so os.killpg won't target real processes
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
            def __init__(self, _interval, function):
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


class TestParseVerdict:
    """Direct unit tests for _parse_verdict — exercises JSON unwrapping,
    code fence removal, and nested result extraction."""

    def test_plain_json(self):
        assert qp._parse_verdict('{"verdict": "pass"}') == "pass"

    def test_plain_json_critical(self):
        assert qp._parse_verdict('{"verdict": "critical"}') == "critical"

    def test_wrapped_in_result_key(self):
        """claude JSON output wraps the answer in {"result": "..."}."""
        inner = json.dumps({"verdict": "warn"})
        outer = json.dumps({"result": inner})
        assert qp._parse_verdict(outer) == "warn"

    def test_code_fence_json(self):
        raw = '```json\n{"verdict": "pass"}\n```'
        assert qp._parse_verdict(raw) == "pass"

    def test_code_fence_no_lang(self):
        raw = '```\n{"verdict": "critical"}\n```'
        assert qp._parse_verdict(raw) == "critical"

    def test_result_wrapped_code_fence(self):
        """Result key wrapping a code-fenced verdict."""
        inner = '```json\n{"verdict": "warn"}\n```'
        outer = json.dumps({"result": inner})
        assert qp._parse_verdict(outer) == "warn"

    def test_not_json(self):
        assert qp._parse_verdict("this is not json") == "unknown"

    def test_empty_string(self):
        assert qp._parse_verdict("") == "unknown"

    def test_json_no_verdict_key(self):
        assert qp._parse_verdict('{"foo": "bar"}') == "unknown"

    def test_whitespace_padding(self):
        assert qp._parse_verdict('  {"verdict": "pass"}  ') == "pass"


class TestRunReviewer:
    def _make_rc(self, review=True):
        return qp.RoundConfig(name="test", review=review)

    def test_skips_when_review_disabled(self, monkeypatch):
        """Should return immediately when review is not enabled."""
        calls = []
        monkeypatch.setattr(qp.process, "git", lambda *a, **_kw: calls.append(a))
        qp.run_reviewer(1, self._make_rc(review=False), "abc", Path("/tmp"), None)
        assert len(calls) == 0  # no git calls made

    def test_cli_flag_overrides_rc(self, monkeypatch):
        """review_flag=False should override rc.review=True."""
        calls = []
        monkeypatch.setattr(qp.process, "git", lambda *a, **_kw: calls.append(a))
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
        def mock_popen(*args, **_kwargs):
            proc = MagicMock()
            proc.pid = -1
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

        def mock_popen(*args, **_kwargs):
            proc = MagicMock()
            proc.pid = -1
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

    def test_returns_verdict_string(self, tmp_path, monkeypatch):
        """run_reviewer should return the verdict string."""
        monkeypatch.setattr(
            qp.process, "git", _mock_git_fn(stdout="diff content\n"),
        )
        monkeypatch.setattr(qp.process, "TEMPLATE_DIR", tmp_path)
        (tmp_path / "reviewer.md").write_text("Review: DIFF_PLACEHOLDER")
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        def mock_popen(*args, **_kwargs):
            proc = MagicMock()
            proc.pid = -1
            proc.stdout = io.StringIO(json.dumps({"verdict": "critical"}))
            proc.stderr = io.StringIO("")
            proc.returncode = 0
            proc.wait.return_value = 0
            return proc

        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        result = qp.run_reviewer(
            1, qp.RoundConfig(name="test", review=True), "abc", log_dir, None,
        )
        assert result == "critical"

    def test_diff_truncation(self, tmp_path, monkeypatch):
        """Large diffs should be truncated to 8000 chars."""
        big_diff = "x" * 10000
        monkeypatch.setattr(
            qp.process, "git", _mock_git_fn(stdout=big_diff),
        )
        monkeypatch.setattr(qp.process, "TEMPLATE_DIR", tmp_path)
        (tmp_path / "reviewer.md").write_text("Review: DIFF_PLACEHOLDER")
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        captured_prompt = []
        def mock_popen(cmd, **_kwargs):
            captured_prompt.append(cmd)
            proc = MagicMock()
            proc.pid = -1
            proc.stdout = io.StringIO(json.dumps({"verdict": "pass"}))
            proc.stderr = io.StringIO("")
            proc.returncode = 0
            proc.wait.return_value = 0
            return proc

        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        qp.run_reviewer(
            1, qp.RoundConfig(name="test", review=True), "abc", log_dir, None,
        )
        # The prompt should contain the truncation message
        prompt = captured_prompt[0][2]  # claude -p <prompt>
        assert "truncated" in prompt

    def test_timeout_returns_none(self, tmp_path, monkeypatch):
        """Reviewer timeout should return None."""
        monkeypatch.setattr(
            qp.process, "git", _mock_git_fn(stdout="diff content\n"),
        )
        monkeypatch.setattr(qp.process, "TEMPLATE_DIR", tmp_path)
        (tmp_path / "reviewer.md").write_text("Review: DIFF_PLACEHOLDER")
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        class InstantTimer:
            def __init__(self, _interval, function):
                self._fn = function
            def start(self):
                self._fn()
            def cancel(self):
                pass

        monkeypatch.setattr(threading, "Timer", InstantTimer)
        def mock_popen(*args, **_kwargs):
            proc = MagicMock()
            proc.pid = -1
            proc.stdout = io.StringIO("")
            proc.stderr = io.StringIO("")
            proc.returncode = -9
            proc.wait.return_value = -9
            return proc

        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        result = qp.run_reviewer(
            1, qp.RoundConfig(name="test", review=True), "abc", log_dir, None,
        )
        assert result is None
