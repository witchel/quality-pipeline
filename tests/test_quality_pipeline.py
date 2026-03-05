"""Tests for quality_pipeline.py — pure-logic and light-filesystem functions."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

# Import the script as a module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import quality_pipeline as qp


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# gate_label
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_valid_frontmatter(self, tmp_path):
        f = tmp_path / "round.md"
        f.write_text(
            "---\n"
            "name: audit-tests\n"
            "gate: soft\n"
            "max_budget_usd: 3.50\n"
            "max_turns: 15\n"
            "max_retries: 2\n"
            "review: true\n"
            "analyzers: bandit semgrep\n"
            "commit_message_prefix: 'test: '\n"
            "---\n"
            "Do the audit.\n"
        )
        rc = qp.parse_frontmatter(f)
        assert rc.name == "audit-tests"
        assert rc.gate == "soft"
        assert rc.max_budget_usd == 3.50
        assert rc.max_turns == 15
        assert rc.max_retries == 2
        assert rc.review is True
        assert rc.analyzers == "bandit semgrep"
        assert rc.commit_message_prefix == "test: "

    def test_missing_frontmatter(self, tmp_path):
        f = tmp_path / "round.md"
        f.write_text("Just a prompt with no frontmatter.\n")
        rc = qp.parse_frontmatter(f)
        assert rc.name == ""
        assert rc.gate == "hard"
        assert rc.max_budget_usd == 5.00

    def test_defaults(self, tmp_path):
        f = tmp_path / "round.md"
        f.write_text("---\nname: minimal\n---\nPrompt here.\n")
        rc = qp.parse_frontmatter(f)
        assert rc.name == "minimal"
        assert rc.gate == "hard"
        assert rc.max_budget_usd == 5.00
        assert rc.max_turns == 20
        assert rc.max_retries == 0
        assert rc.review is None
        assert rc.analyzers == ""
        assert rc.commit_message_prefix == "chore: "

    def test_invalid_yaml(self, tmp_path):
        f = tmp_path / "round.md"
        f.write_text("---\n: [invalid yaml\n---\nPrompt.\n")
        rc = qp.parse_frontmatter(f)
        assert rc.name == ""

    def test_review_string_true(self, tmp_path):
        f = tmp_path / "round.md"
        f.write_text("---\nname: r\nreview: 'true'\n---\n")
        rc = qp.parse_frontmatter(f)
        assert rc.review is True

    def test_review_string_false(self, tmp_path):
        f = tmp_path / "round.md"
        f.write_text("---\nname: r\nreview: 'false'\n---\n")
        rc = qp.parse_frontmatter(f)
        assert rc.review is False

    def test_review_bool(self, tmp_path):
        f = tmp_path / "round.md"
        f.write_text("---\nname: r\nreview: true\n---\n")
        rc = qp.parse_frontmatter(f)
        assert rc.review is True


# ---------------------------------------------------------------------------
# get_round_prompt
# ---------------------------------------------------------------------------


class TestGetRoundPrompt:
    def test_with_frontmatter(self, tmp_path):
        f = tmp_path / "round.md"
        f.write_text("---\nname: test\n---\nDo the thing.\n")
        assert qp.get_round_prompt(f) == "Do the thing.\n"

    def test_no_frontmatter(self, tmp_path):
        f = tmp_path / "round.md"
        f.write_text("Just a plain prompt.\n")
        assert qp.get_round_prompt(f) == "Just a plain prompt.\n"

    def test_leading_newlines_stripped(self, tmp_path):
        f = tmp_path / "round.md"
        f.write_text("---\nname: test\n---\n\n\nPrompt body.\n")
        assert qp.get_round_prompt(f) == "Prompt body.\n"


# ---------------------------------------------------------------------------
# _find_override
# ---------------------------------------------------------------------------


class TestFindOverride:
    def _config_with(self, overrides):
        return qp.PipelineConfig(overrides=overrides)

    def test_exact_match(self):
        cfg = self._config_with({"audit-tests": {"gate": "soft"}})
        assert qp._find_override("audit-tests", cfg) == {"gate": "soft"}

    def test_dash_underscore_normalization(self):
        cfg = self._config_with({"audit_tests": {"gate": "soft"}})
        assert qp._find_override("audit-tests", cfg) == {"gate": "soft"}

    def test_underscore_to_dash(self):
        cfg = self._config_with({"audit-tests": {"gate": "soft"}})
        assert qp._find_override("audit_tests", cfg) == {"gate": "soft"}

    def test_no_match(self):
        cfg = self._config_with({"other": {"gate": "soft"}})
        assert qp._find_override("audit-tests", cfg) == {}

    def test_empty_overrides(self):
        cfg = self._config_with({})
        assert qp._find_override("anything", cfg) == {}


# ---------------------------------------------------------------------------
# apply_config_overrides
# ---------------------------------------------------------------------------


class TestApplyConfigOverrides:
    def test_no_override(self):
        rc = qp.RoundConfig(name="test")
        cfg = qp.PipelineConfig()
        result = qp.apply_config_overrides(rc, cfg)
        assert result.gate == "hard"
        assert result.max_budget_usd == 5.00

    def test_partial_override(self):
        rc = qp.RoundConfig(name="security")
        cfg = qp.PipelineConfig(overrides={
            "security": {"gate": "soft", "max_retries": 3}
        })
        result = qp.apply_config_overrides(rc, cfg)
        assert result.gate == "soft"
        assert result.max_retries == 3
        assert result.max_budget_usd == 5.00  # unchanged

    def test_all_fields(self):
        rc = qp.RoundConfig(name="test")
        cfg = qp.PipelineConfig(overrides={
            "test": {
                "max_budget_usd": 10.0,
                "gate": "none",
                "max_retries": 5,
                "review": True,
                "analyzers": "mypy pyright",
            }
        })
        result = qp.apply_config_overrides(rc, cfg)
        assert result.max_budget_usd == 10.0
        assert result.gate == "none"
        assert result.max_retries == 5
        assert result.review is True
        assert result.analyzers == "mypy pyright"

    def test_review_string_override(self):
        rc = qp.RoundConfig(name="test")
        cfg = qp.PipelineConfig(overrides={"test": {"review": "true"}})
        result = qp.apply_config_overrides(rc, cfg)
        assert result.review is True

    def test_dash_underscore_lookup(self):
        rc = qp.RoundConfig(name="dead-code")
        cfg = qp.PipelineConfig(overrides={"dead_code": {"gate": "none"}})
        result = qp.apply_config_overrides(rc, cfg)
        assert result.gate == "none"


# ---------------------------------------------------------------------------
# apply_config_prompt_append
# ---------------------------------------------------------------------------


class TestApplyConfigPromptAppend:
    def test_no_override(self):
        cfg = qp.PipelineConfig()
        result = qp.apply_config_prompt_append("test", "base prompt", cfg)
        assert result == "base prompt"

    def test_override_without_append(self):
        cfg = qp.PipelineConfig(overrides={"test": {"gate": "soft"}})
        result = qp.apply_config_prompt_append("test", "base prompt", cfg)
        assert result == "base prompt"

    def test_append_prompt(self):
        cfg = qp.PipelineConfig(overrides={
            "test": {"append_prompt": "Also do this."}
        })
        result = qp.apply_config_prompt_append("test", "base prompt", cfg)
        assert result == "base prompt\n\nAlso do this."


# ---------------------------------------------------------------------------
# load_pipeline_config
# ---------------------------------------------------------------------------


class TestLoadPipelineConfig:
    def test_valid_config(self, tmp_path):
        f = tmp_path / "pipeline.yaml"
        f.write_text(yaml.dump({
            "test_command": "pytest",
            "branch_prefix": "quality",
            "rounds": ["audit-tests", "refactor"],
            "max_budget_usd": 10.0,
            "overrides": {
                "audit-tests": {"gate": "soft", "max_retries": 2},
            },
        }))
        cfg = qp.load_pipeline_config(f)
        assert cfg.test_command == "pytest"
        assert cfg.branch_prefix == "quality"
        assert cfg.rounds == ["audit-tests", "refactor"]
        assert cfg.max_budget_usd == 10.0
        assert "audit-tests" in cfg.overrides
        assert cfg.overrides["audit-tests"]["gate"] == "soft"

    def test_empty_file(self, tmp_path):
        f = tmp_path / "pipeline.yaml"
        f.write_text("")
        cfg = qp.load_pipeline_config(f)
        assert cfg.test_command == ""
        assert cfg.rounds == []

    def test_invalid_yaml(self, tmp_path):
        f = tmp_path / "pipeline.yaml"
        f.write_text(": [broken\n")
        cfg = qp.load_pipeline_config(f)
        assert cfg.test_command == ""

    def test_missing_file(self, tmp_path):
        f = tmp_path / "nonexistent.yaml"
        cfg = qp.load_pipeline_config(f)
        assert cfg.test_command == ""

    def test_non_dict_overrides_ignored(self, tmp_path):
        f = tmp_path / "pipeline.yaml"
        f.write_text(yaml.dump({
            "overrides": {
                "valid": {"gate": "soft"},
                "invalid": "not a dict",
            }
        }))
        cfg = qp.load_pipeline_config(f)
        assert "valid" in cfg.overrides
        assert "invalid" not in cfg.overrides


# ---------------------------------------------------------------------------
# detect_test_command
# ---------------------------------------------------------------------------


class TestDetectTestCommand:
    def test_claude_md_explicit(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("test command: npm run test:ci\n")
        assert qp.detect_test_command(tmp_path) == "npm run test:ci"

    def test_claude_md_backtick(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("Run `pytest -x` to check.\n")
        assert qp.detect_test_command(tmp_path) == "pytest -x"

    def test_makefile_with_test_target(self, tmp_path):
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
        assert qp.detect_test_command(tmp_path) == "make test"

    def test_makefile_without_test_target(self, tmp_path):
        (tmp_path / "Makefile").write_text("build:\n\tgcc main.c\n")
        assert qp.detect_test_command(tmp_path) is None

    def test_package_json_npm(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "jest"}
        }))
        assert qp.detect_test_command(tmp_path) == "npm test"

    def test_package_json_bun(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "jest"}
        }))
        (tmp_path / "bun.lockb").write_bytes(b"")
        assert qp.detect_test_command(tmp_path) == "bun test"

    def test_package_json_pnpm(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "vitest"}
        }))
        (tmp_path / "pnpm-lock.yaml").write_text("")
        assert qp.detect_test_command(tmp_path) == "pnpm test"

    def test_package_json_yarn(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "jest"}
        }))
        (tmp_path / "yarn.lock").write_text("")
        assert qp.detect_test_command(tmp_path) == "yarn test"

    def test_package_json_no_test_specified(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "echo \"Error: no test specified\" && exit 1"}
        }))
        assert qp.detect_test_command(tmp_path) is None

    def test_pyproject_with_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
        (tmp_path / "uv.lock").write_text("")
        assert qp.detect_test_command(tmp_path) == "uv run pytest"

    def test_pyproject_with_pytest_no_uv(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
        assert qp.detect_test_command(tmp_path) == "pytest"

    def test_setup_cfg_with_pytest(self, tmp_path):
        (tmp_path / "setup.cfg").write_text("[tool:pytest]\n")
        assert qp.detect_test_command(tmp_path) == "pytest"

    def test_tests_dir_with_python_markers(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "requirements.txt").write_text("pytest\n")
        (tmp_path / "uv.lock").write_text("")
        assert qp.detect_test_command(tmp_path) == "uv run pytest"

    def test_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/foo\n")
        assert qp.detect_test_command(tmp_path) == "go test ./..."

    def test_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname = \"foo\"\n")
        assert qp.detect_test_command(tmp_path) == "cargo test"

    def test_empty_project(self, tmp_path):
        assert qp.detect_test_command(tmp_path) is None

    def test_claude_md_takes_priority(self, tmp_path):
        """CLAUDE.md should win over Makefile and package.json."""
        (tmp_path / "CLAUDE.md").write_text("test command: make check\n")
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "jest"}
        }))
        assert qp.detect_test_command(tmp_path) == "make check"

    def test_bun_lock_json(self, tmp_path):
        """bun.lock (JSON format) should also trigger bun."""
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "jest"}
        }))
        (tmp_path / "bun.lock").write_text("{}")
        assert qp.detect_test_command(tmp_path) == "bun test"


# ---------------------------------------------------------------------------
# _parse_review_bool
# ---------------------------------------------------------------------------


class TestParseReviewBool:
    def test_bool_true(self):
        assert qp._parse_review_bool(True) is True

    def test_bool_false(self):
        assert qp._parse_review_bool(False) is False

    def test_str_true_lower(self):
        assert qp._parse_review_bool("true") is True

    def test_str_true_mixed(self):
        assert qp._parse_review_bool("True") is True

    def test_str_false(self):
        assert qp._parse_review_bool("false") is False

    def test_str_other(self):
        assert qp._parse_review_bool("yes") is False

    def test_none_returns_none(self):
        assert qp._parse_review_bool(None) is None

    def test_int_returns_none(self):
        assert qp._parse_review_bool(1) is None


# ---------------------------------------------------------------------------
# _find_override — additional edge cases
# ---------------------------------------------------------------------------


class TestFindOverrideEdgeCases:
    def _config_with(self, overrides):
        return qp.PipelineConfig(overrides=overrides)

    def test_empty_dict_value_exact_match(self):
        """An explicit empty override should be returned via the fast path."""
        cfg = self._config_with({"audit-tests": {}})
        assert qp._find_override("audit-tests", cfg) == {}

    def test_empty_dict_value_normalized(self):
        """Empty override found via dash/underscore normalization."""
        cfg = self._config_with({"audit_tests": {}})
        assert qp._find_override("audit-tests", cfg) == {}


# ---------------------------------------------------------------------------
# ColorOutput
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# discover_rounds / resolve_round_file
# ---------------------------------------------------------------------------


class TestDiscoverRounds:
    def test_finds_md_files_sorted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp, "ROUNDS_DIR", tmp_path)
        (tmp_path / "02-second.md").write_text("---\nname: second\n---\n")
        (tmp_path / "01-first.md").write_text("---\nname: first\n---\n")
        (tmp_path / "readme.txt").write_text("not a round")
        result = qp.discover_rounds()
        assert len(result) == 2
        assert result[0].name == "01-first.md"
        assert result[1].name == "02-second.md"

    def test_empty_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp, "ROUNDS_DIR", tmp_path)
        assert qp.discover_rounds() == []

    def test_missing_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp, "ROUNDS_DIR", tmp_path / "nonexistent")
        assert qp.discover_rounds() == []


class TestResolveRoundFile:
    def test_match_by_frontmatter_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp, "ROUNDS_DIR", tmp_path)
        f = tmp_path / "01-audit.md"
        f.write_text("---\nname: audit-tests\n---\nDo audit.\n")
        assert qp.resolve_round_file("audit-tests") == f

    def test_match_by_filename_dash_pattern(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp, "ROUNDS_DIR", tmp_path)
        f = tmp_path / "01-security.md"
        f.write_text("---\nname: something-else\n---\n")
        assert qp.resolve_round_file("security") == f

    def test_match_by_filename_glob(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp, "ROUNDS_DIR", tmp_path)
        f = tmp_path / "refactor-code.md"
        f.write_text("---\nname: other\n---\n")
        assert qp.resolve_round_file("refactor") == f

    def test_no_match(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp, "ROUNDS_DIR", tmp_path)
        (tmp_path / "01-audit.md").write_text("---\nname: audit\n---\n")
        assert qp.resolve_round_file("nonexistent") is None


# ---------------------------------------------------------------------------
# run_tests_with_tee
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# git helper functions (monkeypatched)
# ---------------------------------------------------------------------------


def _mock_git_fn(**defaults):
    """Create a mock for qp.git that returns a configurable result."""
    def mock_git(*args, **kwargs):
        r = MagicMock()
        r.returncode = defaults.get("returncode", 0)
        r.stdout = defaults.get("stdout", "")
        r.stderr = defaults.get("stderr", "")
        return r
    return mock_git


class TestGitHasUncommitted:
    def test_clean_repo(self, monkeypatch):
        monkeypatch.setattr(qp, "git", _mock_git_fn(returncode=0))
        assert qp.git_has_uncommitted() is False

    def test_unstaged_changes(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            # First call (git diff --quiet) fails, second succeeds
            r.returncode = 1 if len(calls) == 1 else 0
            return r
        monkeypatch.setattr(qp, "git", mock_git)
        assert qp.git_has_uncommitted() is True

    def test_staged_changes(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            # First call succeeds, second (--cached) fails
            r.returncode = 0 if len(calls) == 1 else 1
            return r
        monkeypatch.setattr(qp, "git", mock_git)
        assert qp.git_has_uncommitted() is True


class TestGitUntrackedFiles:
    def test_no_untracked(self, monkeypatch):
        monkeypatch.setattr(qp, "git", _mock_git_fn(stdout=""))
        assert qp.git_untracked_files() == set()

    def test_some_untracked(self, monkeypatch):
        monkeypatch.setattr(qp, "git", _mock_git_fn(stdout="a.py\nb.py\n"))
        assert qp.git_untracked_files() == {"a.py", "b.py"}

    def test_whitespace_only(self, monkeypatch):
        monkeypatch.setattr(qp, "git", _mock_git_fn(stdout="  \n"))
        assert qp.git_untracked_files() == set()


class TestGitStageRoundChanges:
    def test_stages_modified_and_new(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            r.stdout = "new_file.py\n" if "ls-files" in args else ""
            return r
        monkeypatch.setattr(qp, "git", mock_git)
        qp.git_stage_round_changes(set())
        # Should have: add -u, ls-files, add -- new_file.py
        add_calls = [c for c in calls if c[0] == "add"]
        assert any("-u" in c for c in add_calls)
        assert any("new_file.py" in c for c in add_calls)

    def test_does_not_stage_preexisting_untracked(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            r.stdout = "old.py\nnew.py\n" if "ls-files" in args else ""
            return r
        monkeypatch.setattr(qp, "git", mock_git)
        qp.git_stage_round_changes({"old.py"})
        # Only new.py should be added, not old.py
        add_file_calls = [c for c in calls if len(c) >= 3 and c[0] == "add" and c[1] == "--"]
        assert len(add_file_calls) == 1
        assert add_file_calls[0][2] == "new.py"


class TestGitRollbackRound:
    def test_resets_and_removes_new_files(self, tmp_path, monkeypatch):
        new_file = tmp_path / "new.py"
        new_file.write_text("content")
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            r.stdout = str(new_file) + "\n" if "ls-files" in args else ""
            return r
        monkeypatch.setattr(qp, "git", mock_git)
        qp.git_rollback_round(set())
        # Should call reset and checkout
        assert any("reset" in c for c in calls)
        assert any("checkout" in c for c in calls)


class TestGitCreateBranch:
    def test_creates_new_branch(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            # show-ref fails (branch doesn't exist)
            r.returncode = 1 if "show-ref" in args else 0
            return r
        monkeypatch.setattr(qp, "git", mock_git)
        qp.git_create_branch("quality/test")
        assert any("checkout" in c and "-b" in c for c in calls)

    def test_uses_existing_branch(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            r.returncode = 0
            return r
        monkeypatch.setattr(qp, "git", mock_git)
        qp.git_create_branch("quality/test")
        # Should checkout without -b
        checkout_calls = [c for c in calls if "checkout" in c]
        assert any("-b" not in c for c in checkout_calls)


class TestGitCommit:
    def test_success_with_no_gpg_sign(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            r.returncode = 0
            return r
        monkeypatch.setattr(qp, "git", mock_git)
        qp.git_commit("test msg")
        assert len(calls) == 1
        assert "--no-gpg-sign" in calls[0]

    def test_gpg_fallback(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            if len(calls) == 1:
                r.returncode = 1
                r.stderr = "error: gpg failed to sign"
            else:
                r.returncode = 0
            return r
        monkeypatch.setattr(qp, "git", mock_git)
        qp.git_commit("test msg")
        assert len(calls) == 2
        assert "--no-gpg-sign" not in calls[1]

    def test_non_gpg_failure_raises(self, monkeypatch):
        def mock_git(*args, **kwargs):
            r = MagicMock()
            r.returncode = 1
            r.stderr = "error: pathspec not found"
            return r
        monkeypatch.setattr(qp, "git", mock_git)
        with pytest.raises(subprocess.CalledProcessError):
            qp.git_commit("test msg")


# ---------------------------------------------------------------------------
# git_acquire_lock
# ---------------------------------------------------------------------------


class TestGitAcquireLock:
    def test_dry_run_returns_none(self):
        assert qp.git_acquire_lock(True) is None

    def test_creates_lock_dir(self, tmp_path, monkeypatch):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        monkeypatch.setattr(qp, "git", _mock_git_fn(stdout=str(git_dir) + "\n"))
        result = qp.git_acquire_lock(False)
        assert result is not None
        expected = git_dir / "quality-pipeline.lock"
        assert result == expected
        assert result.is_dir()
        # Cleanup
        result.rmdir()

    def test_concurrent_lock_exits(self, tmp_path, monkeypatch):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "quality-pipeline.lock").mkdir()
        monkeypatch.setattr(qp, "git", _mock_git_fn(stdout=str(git_dir) + "\n"))
        with pytest.raises(SystemExit):
            qp.git_acquire_lock(False)


# ---------------------------------------------------------------------------
# PipelineCleanup
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _run_analyzer / run_static_analysis
# ---------------------------------------------------------------------------


class TestRunAnalyzer:
    def test_tool_not_found(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert qp._run_analyzer("missing", ["missing", "."], Path(".")) == ""

    def test_prerequisite_not_met(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        result = qp._run_analyzer(
            "bandit", ["bandit", "."], tmp_path, ["pyproject.toml"]
        )
        assert result == ""

    def test_prerequisite_met_runs_command(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("")
        def mock_which(name):
            if name in ("gtimeout", "timeout"):
                return None
            return f"/usr/bin/{name}"
        monkeypatch.setattr(shutil, "which", mock_which)
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: MagicMock(stdout="  finding1  ", returncode=0),
        )
        result = qp._run_analyzer(
            "bandit", ["bandit", "."], tmp_path, ["pyproject.toml"]
        )
        assert result == "finding1"

    def test_command_exception_returns_empty(self, tmp_path, monkeypatch):
        def mock_which(name):
            if name in ("gtimeout", "timeout"):
                return None
            return f"/usr/bin/{name}"
        monkeypatch.setattr(shutil, "which", mock_which)
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("boom")),
        )
        assert qp._run_analyzer("mypy", ["mypy", "."], tmp_path) == ""

    def test_timeout_prefix_gtimeout(self, tmp_path, monkeypatch):
        run_calls = []
        def mock_which(name):
            return f"/usr/bin/{name}"
        def mock_run(*args, **kwargs):
            run_calls.append(args[0])
            return MagicMock(stdout="ok", returncode=0)
        monkeypatch.setattr(shutil, "which", mock_which)
        monkeypatch.setattr(subprocess, "run", mock_run)
        qp._run_analyzer("mypy", ["mypy", "."], tmp_path)
        assert run_calls[0][0] == "gtimeout"


class TestRunStaticAnalysis:
    def test_unknown_round_no_analyzers(self):
        assert qp.run_static_analysis("unknown-round", Path(".")) == ""

    def test_override_analyzers(self, monkeypatch):
        monkeypatch.setattr(
            qp, "_run_analyzer",
            lambda name, args, proj, prereqs=None: f"output-{name}",
        )
        result = qp.run_static_analysis("any-round", Path("."), "mypy vulture")
        assert "### mypy" in result
        assert "output-mypy" in result
        assert "### vulture" in result

    def test_truncation(self, monkeypatch):
        monkeypatch.setattr(
            qp, "_run_analyzer",
            lambda name, args, proj, prereqs=None: "x" * 5000,
        )
        result = qp.run_static_analysis("security", Path("."))
        assert len(result) <= qp.MAX_ANALYSIS_OUTPUT

    def test_unknown_analyzer_skipped(self, monkeypatch):
        monkeypatch.setattr(
            qp, "_run_analyzer",
            lambda name, args, proj, prereqs=None: "found",
        )
        result = qp.run_static_analysis("any", Path("."), "nonexistent_tool")
        assert result == ""


# ---------------------------------------------------------------------------
# _handle_signal
# ---------------------------------------------------------------------------


class TestHandleSignal:
    def test_exits_with_128_plus_signum(self):
        with pytest.raises(SystemExit) as exc_info:
            qp._handle_signal(15, None)
        assert exc_info.value.code == 128 + 15

    def test_sigint_code(self):
        with pytest.raises(SystemExit) as exc_info:
            qp._handle_signal(2, None)
        assert exc_info.value.code == 130


# ---------------------------------------------------------------------------
# RoundOutcome / RoundResult
# ---------------------------------------------------------------------------


class TestRoundOutcome:
    def test_values(self):
        assert qp.RoundOutcome.PASSED.value == "passed"
        assert qp.RoundOutcome.NO_CHANGES.value == "no-changes"
        assert qp.RoundOutcome.HARD_FAILED.value == "HARD-FAILED"
        assert qp.RoundOutcome.SOFT_FAILED.value == "soft-failed"
        assert qp.RoundOutcome.SKIPPED.value == "skipped"

    def test_round_result(self):
        r = qp.RoundResult("test", qp.RoundOutcome.PASSED)
        assert r.name == "test"
        assert r.outcome == qp.RoundOutcome.PASSED


# ---------------------------------------------------------------------------
# ResourceMonitor
# ---------------------------------------------------------------------------


class TestResourceMonitor:
    def test_start_and_stop(self, monkeypatch):
        """Monitor should start and stop cleanly without errors."""
        import time
        monkeypatch.setattr(
            qp, "get_resource_snapshot", lambda gpu_type="none": "CPU: ok"
        )
        monitor = qp.ResourceMonitor(
            interval=1, gpu_type="none", round_name="test", start_epoch=time.time()
        )
        monitor.start()
        assert monitor._thread.is_alive()
        monitor.stop()
        assert not monitor._thread.is_alive()


# ---------------------------------------------------------------------------
# run_reviewer — verdict parsing
# ---------------------------------------------------------------------------


class TestRunReviewer:
    def _make_rc(self, review=True):
        return qp.RoundConfig(name="test", review=review)

    def test_skips_when_review_disabled(self, monkeypatch):
        """Should return immediately when review is not enabled."""
        calls = []
        monkeypatch.setattr(qp, "git", lambda *a, **kw: calls.append(a))
        qp.run_reviewer(1, self._make_rc(review=False), "abc", Path("/tmp"), None)
        assert len(calls) == 0  # no git calls made

    def test_cli_flag_overrides_rc(self, monkeypatch):
        """review_flag=False should override rc.review=True."""
        calls = []
        monkeypatch.setattr(qp, "git", lambda *a, **kw: calls.append(a))
        qp.run_reviewer(1, self._make_rc(review=True), "abc", Path("/tmp"), False)
        assert len(calls) == 0

    def test_cli_flag_enables_review(self, tmp_path, monkeypatch):
        """review_flag=True should enable review even when rc.review=False."""
        monkeypatch.setattr(
            qp, "git",
            _mock_git_fn(stdout="diff content here\n"),
        )
        monkeypatch.setattr(qp, "TEMPLATE_DIR", tmp_path)
        (tmp_path / "reviewer.md").write_text("Review: DIFF_PLACEHOLDER")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        qp.run_reviewer(
            1, self._make_rc(review=False), "abc", log_dir, True,
        )
        assert list(log_dir.glob("review-*.json"))

    def test_no_diff_skips_review(self, tmp_path, monkeypatch):
        """When diff is empty, reviewer should skip."""
        monkeypatch.setattr(qp, "git", _mock_git_fn(stdout=""))
        monkeypatch.setattr(qp, "TEMPLATE_DIR", tmp_path)
        (tmp_path / "reviewer.md").write_text("Review: DIFF_PLACEHOLDER")
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        qp.run_reviewer(1, self._make_rc(), "abc", log_dir, None)
        assert not list(log_dir.glob("review-*.json"))

    def test_missing_template_skips(self, tmp_path, monkeypatch):
        """When template file is missing, reviewer should skip."""
        monkeypatch.setattr(
            qp, "git", _mock_git_fn(stdout="some diff\n"),
        )
        monkeypatch.setattr(qp, "TEMPLATE_DIR", tmp_path / "nonexistent")
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        qp.run_reviewer(1, self._make_rc(), "abc", log_dir, None)
        assert not list(log_dir.glob("review-*.json"))

    def _run_reviewer_with_verdict(self, tmp_path, monkeypatch, verdict_json):
        """Helper: run reviewer and write a specific verdict to the output file."""
        monkeypatch.setattr(
            qp, "git", _mock_git_fn(stdout="diff content\n"),
        )
        monkeypatch.setattr(qp, "TEMPLATE_DIR", tmp_path)
        (tmp_path / "reviewer.md").write_text("Review: DIFF_PLACEHOLDER")

        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        def mock_subprocess_run(*args, **kwargs):
            # Write the verdict to the output file that run_reviewer opens
            stdout_file = kwargs.get("stdout")
            if stdout_file and hasattr(stdout_file, "write"):
                stdout_file.write(verdict_json)
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_subprocess_run)
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


# ---------------------------------------------------------------------------
# run_claude
# ---------------------------------------------------------------------------


class TestRunClaude:
    def test_returns_exit_code(self, tmp_path, monkeypatch):
        log_file = tmp_path / "claude.log"
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        code = qp.run_claude("prompt", "ctx", 5.0, 20, log_file)
        assert code == 0

    def test_returns_nonzero(self, tmp_path, monkeypatch):
        log_file = tmp_path / "claude.log"
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=1),
        )
        code = qp.run_claude("prompt", "ctx", 5.0, 20, log_file)
        assert code == 1

    def test_passes_budget_and_turns(self, tmp_path, monkeypatch):
        log_file = tmp_path / "claude.log"
        captured_cmd = []
        def mock_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return MagicMock(returncode=0)
        monkeypatch.setattr(subprocess, "run", mock_run)
        qp.run_claude("prompt", "ctx", 3.50, 10, log_file)
        assert "3.50" in captured_cmd
        assert "10" in captured_cmd


# ---------------------------------------------------------------------------
# get_resource_snapshot (integration — runs on current platform)
# ---------------------------------------------------------------------------


class TestGetResourceSnapshot:
    def test_returns_string(self):
        result = qp.get_resource_snapshot()
        assert isinstance(result, str)
        assert "CPU:" in result
        assert "Mem:" in result

    def test_with_gpu_none(self):
        result = qp.get_resource_snapshot("none")
        assert "GPU" not in result


# ---------------------------------------------------------------------------
# detect_gpu
# ---------------------------------------------------------------------------


class TestDetectGpu:
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
            lambda *a, **kw: (_ for _ in ()).throw(
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


# ---------------------------------------------------------------------------
# git_rev_parse_head
# ---------------------------------------------------------------------------


class TestGitRevParseHead:
    def test_returns_sha(self, monkeypatch):
        monkeypatch.setattr(qp, "git", _mock_git_fn(stdout="abc123\n"))
        assert qp.git_rev_parse_head() == "abc123"


# ---------------------------------------------------------------------------
# run_round (integration tests with mocked externals)
# ---------------------------------------------------------------------------


class TestRunRound:
    """Tests for run_round with mocked externals."""

    @pytest.fixture
    def round_file(self, tmp_path):
        f = tmp_path / "01-test.md"
        f.write_text("---\nname: test-round\ngate: hard\n---\nDo stuff.\n")
        return f

    @pytest.fixture
    def log_dir(self, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        return d

    @pytest.fixture
    def mock_env(self, monkeypatch):
        """Set up common mocks for run_round tests."""
        monkeypatch.setattr(qp, "git_rev_parse_head", lambda: "abc123")
        monkeypatch.setattr(qp, "git_untracked_files", lambda: set())
        monkeypatch.setattr(
            qp, "get_resource_snapshot", lambda gpu_type="none": "CPU: ok"
        )
        monkeypatch.setattr(
            qp, "run_static_analysis", lambda *a, **kw: ""
        )
        monkeypatch.setattr(qp, "git_stage_round_changes", lambda pre: None)
        monkeypatch.setattr(qp, "git_commit", lambda msg: None)
        monkeypatch.setattr(qp, "run_reviewer", lambda *a, **kw: None)
        # Prevent resource monitor from actually running
        monkeypatch.setattr(
            qp.ResourceMonitor, "start", lambda self: None,
        )
        monkeypatch.setattr(
            qp.ResourceMonitor, "stop", lambda self: None,
        )

    def test_claude_failure_hard_gate(
        self, round_file, log_dir, mock_env, monkeypatch
    ):
        monkeypatch.setattr(qp, "run_claude", lambda *a, **kw: 1)
        result = qp.run_round(
            round_file, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.HARD_FAILED

    def test_claude_failure_soft_gate(
        self, tmp_path, log_dir, mock_env, monkeypatch
    ):
        f = tmp_path / "01-soft.md"
        f.write_text("---\nname: soft-round\ngate: soft\n---\nDo stuff.\n")
        monkeypatch.setattr(qp, "run_claude", lambda *a, **kw: 1)
        result = qp.run_round(
            f, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.SOFT_FAILED

    def test_no_changes(self, round_file, log_dir, mock_env, monkeypatch):
        monkeypatch.setattr(qp, "run_claude", lambda *a, **kw: 0)
        # git diff --quiet returns 0 (no changes)
        monkeypatch.setattr(qp, "git", _mock_git_fn(returncode=0))
        result = qp.run_round(
            round_file, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.NO_CHANGES

    def test_gate_none_skips_tests(
        self, tmp_path, log_dir, mock_env, monkeypatch
    ):
        f = tmp_path / "01-none.md"
        f.write_text("---\nname: none-round\ngate: none\n---\nDo stuff.\n")
        monkeypatch.setattr(qp, "run_claude", lambda *a, **kw: 0)
        # Simulate changes exist
        monkeypatch.setattr(qp, "git", _mock_git_fn(returncode=1))
        test_calls = []
        monkeypatch.setattr(
            qp, "run_tests_with_tee",
            lambda *a: test_calls.append(1) or 0,
        )
        result = qp.run_round(
            f, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.PASSED
        assert len(test_calls) == 0  # tests should not have been run

    def test_tests_pass(self, round_file, log_dir, mock_env, monkeypatch):
        monkeypatch.setattr(qp, "run_claude", lambda *a, **kw: 0)
        monkeypatch.setattr(qp, "git", _mock_git_fn(returncode=1))
        monkeypatch.setattr(qp, "run_tests_with_tee", lambda *a: 0)
        result = qp.run_round(
            round_file, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.PASSED

    def test_tests_fail_no_retries(
        self, round_file, log_dir, mock_env, monkeypatch
    ):
        monkeypatch.setattr(qp, "run_claude", lambda *a, **kw: 0)
        monkeypatch.setattr(qp, "git", _mock_git_fn(returncode=1))
        monkeypatch.setattr(qp, "run_tests_with_tee", lambda cmd, f: 1)
        monkeypatch.setattr(qp, "git_rollback_round", lambda pre: None)
        result = qp.run_round(
            round_file, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.HARD_FAILED

    def test_tests_fail_then_retry_succeeds(
        self, tmp_path, log_dir, mock_env, monkeypatch
    ):
        f = tmp_path / "01-retry.md"
        f.write_text(
            "---\nname: retry-round\ngate: hard\nmax_retries: 1\n---\nDo stuff.\n"
        )
        monkeypatch.setattr(qp, "run_claude", lambda *a, **kw: 0)
        monkeypatch.setattr(qp, "git", _mock_git_fn(returncode=1))
        test_attempts = []
        def mock_tests(cmd, output_file):
            test_attempts.append(1)
            # Write something so retry can read it
            output_file.write_text("FAIL: test_foo")
            return 1 if len(test_attempts) == 1 else 0
        monkeypatch.setattr(qp, "run_tests_with_tee", mock_tests)
        result = qp.run_round(
            f, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.PASSED
        assert len(test_attempts) == 2

    def test_tests_fail_soft_gate_rollback(
        self, tmp_path, log_dir, mock_env, monkeypatch
    ):
        f = tmp_path / "01-soft.md"
        f.write_text("---\nname: soft-fail\ngate: soft\n---\nDo stuff.\n")
        monkeypatch.setattr(qp, "run_claude", lambda *a, **kw: 0)
        monkeypatch.setattr(qp, "git", _mock_git_fn(returncode=1))
        monkeypatch.setattr(qp, "run_tests_with_tee", lambda cmd, f: 1)
        rolled_back = []
        monkeypatch.setattr(
            qp, "git_rollback_round", lambda pre: rolled_back.append(1)
        )
        result = qp.run_round(
            f, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.SOFT_FAILED
        assert len(rolled_back) == 1


# ---------------------------------------------------------------------------
# pipeline() — orchestrator integration tests
# ---------------------------------------------------------------------------


class TestPipeline:
    """Tests for the pipeline() orchestrator with mocked dependencies."""

    @pytest.fixture
    def pipeline_env(self, tmp_path, monkeypatch):
        """Set up a minimal environment for pipeline tests."""
        # Create round files
        rounds_dir = tmp_path / "rounds"
        rounds_dir.mkdir()
        (rounds_dir / "01-audit.md").write_text(
            "---\nname: audit\ngate: hard\n---\nAudit stuff.\n"
        )
        monkeypatch.setattr(qp, "ROUNDS_DIR", rounds_dir)

        # We're in a git repo (mock subprocess for git check)
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="", stderr=""),
        )
        # Mock git helper
        monkeypatch.setattr(qp, "git", _mock_git_fn(stdout="abc1234\n"))
        monkeypatch.setattr(qp, "git_acquire_lock", lambda dry_run: None)
        monkeypatch.setattr(qp, "git_has_uncommitted", lambda: False)
        monkeypatch.setattr(qp, "git_create_branch", lambda name: None)
        monkeypatch.setattr(qp, "detect_gpu", lambda: "none")
        monkeypatch.setattr(
            qp, "get_resource_snapshot", lambda gpu_type="none": "CPU: ok"
        )

        # Change to tmp_path so pipeline doesn't operate on real repo
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_dry_run(self, pipeline_env, monkeypatch):
        """Dry run should not call run_round."""
        run_round_calls = []
        monkeypatch.setattr(
            qp, "run_round",
            lambda *a, **kw: run_round_calls.append(1) or qp.RoundOutcome.PASSED,
        )
        qp.pipeline(
            project_dir=None,
            rounds_arg=None,
            config_file=None,
            start_from=1,
            dry_run=True,
            worktree=False,
            worktree_symlinks=None,
            test_command="true",
            review_flag=None,
            log_dir_arg=str(pipeline_env / "logs"),
        )
        assert len(run_round_calls) == 0

    def test_single_round_passes(self, pipeline_env, monkeypatch):
        monkeypatch.setattr(
            qp, "run_round",
            lambda *a, **kw: qp.RoundOutcome.PASSED,
        )
        # Should not raise or sys.exit
        qp.pipeline(
            project_dir=None,
            rounds_arg=None,
            config_file=None,
            start_from=1,
            dry_run=False,
            worktree=False,
            worktree_symlinks=None,
            test_command="true",
            review_flag=None,
            log_dir_arg=str(pipeline_env / "logs"),
        )

    def test_hard_failure_exits_nonzero(self, pipeline_env, monkeypatch):
        monkeypatch.setattr(
            qp, "run_round",
            lambda *a, **kw: qp.RoundOutcome.HARD_FAILED,
        )
        with pytest.raises(SystemExit) as exc_info:
            qp.pipeline(
                project_dir=None,
                rounds_arg=None,
                config_file=None,
                start_from=1,
                dry_run=False,
                worktree=False,
                worktree_symlinks=None,
                test_command="true",
                review_flag=None,
                log_dir_arg=str(pipeline_env / "logs"),
            )
        assert exc_info.value.code == 1

    def test_start_from_skips_rounds(self, tmp_path, pipeline_env, monkeypatch):
        rounds_dir = tmp_path / "rounds"
        (rounds_dir / "02-refactor.md").write_text(
            "---\nname: refactor\ngate: hard\n---\nRefactor stuff.\n"
        )
        round_names = []
        orig_run_round = qp.run_round
        def mock_run_round(rf, n, total, *args, **kwargs):
            rc = qp.parse_frontmatter(rf)
            round_names.append(rc.name)
            return qp.RoundOutcome.PASSED
        monkeypatch.setattr(qp, "run_round", mock_run_round)
        qp.pipeline(
            project_dir=None,
            rounds_arg=None,
            config_file=None,
            start_from=2,
            dry_run=False,
            worktree=False,
            worktree_symlinks=None,
            test_command="true",
            review_flag=None,
            log_dir_arg=str(pipeline_env / "logs"),
        )
        # Only the second round should have been run
        assert round_names == ["refactor"]

    def test_no_test_command_auto_detect_fails(self, pipeline_env, monkeypatch):
        monkeypatch.setattr(qp, "detect_test_command", lambda path: None)
        with pytest.raises(SystemExit):
            qp.pipeline(
                project_dir=None,
                rounds_arg=None,
                config_file=None,
                start_from=1,
                dry_run=False,
                worktree=False,
                worktree_symlinks=None,
                test_command=None,
                review_flag=None,
                log_dir_arg=str(pipeline_env / "logs"),
            )

    def test_no_rounds_found_exits(self, tmp_path, pipeline_env, monkeypatch):
        # Empty rounds dir
        empty_rounds = tmp_path / "empty_rounds"
        empty_rounds.mkdir()
        monkeypatch.setattr(qp, "ROUNDS_DIR", empty_rounds)
        with pytest.raises(SystemExit):
            qp.pipeline(
                project_dir=None,
                rounds_arg=None,
                config_file=None,
                start_from=1,
                dry_run=False,
                worktree=False,
                worktree_symlinks=None,
                test_command="true",
                review_flag=None,
                log_dir_arg=str(pipeline_env / "logs"),
            )

    def test_unknown_round_name_exits(self, pipeline_env, monkeypatch):
        with pytest.raises(SystemExit):
            qp.pipeline(
                project_dir=None,
                rounds_arg="nonexistent-round",
                config_file=None,
                start_from=1,
                dry_run=False,
                worktree=False,
                worktree_symlinks=None,
                test_command="true",
                review_flag=None,
                log_dir_arg=str(pipeline_env / "logs"),
            )

    def test_start_from_exceeds_total(self, pipeline_env, monkeypatch):
        with pytest.raises(SystemExit):
            qp.pipeline(
                project_dir=None,
                rounds_arg=None,
                config_file=None,
                start_from=99,
                dry_run=False,
                worktree=False,
                worktree_symlinks=None,
                test_command="true",
                review_flag=None,
                log_dir_arg=str(pipeline_env / "logs"),
            )

    def test_soft_failure_continues(self, tmp_path, pipeline_env, monkeypatch):
        rounds_dir = tmp_path / "rounds"
        (rounds_dir / "02-refactor.md").write_text(
            "---\nname: refactor\ngate: hard\n---\nRefactor stuff.\n"
        )
        call_count = [0]
        def mock_run_round(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return qp.RoundOutcome.SOFT_FAILED
            return qp.RoundOutcome.PASSED
        monkeypatch.setattr(qp, "run_round", mock_run_round)
        qp.pipeline(
            project_dir=None,
            rounds_arg=None,
            config_file=None,
            start_from=1,
            dry_run=False,
            worktree=False,
            worktree_symlinks=None,
            test_command="true",
            review_flag=None,
            log_dir_arg=str(pipeline_env / "logs"),
        )
        assert call_count[0] == 2

    def test_config_file_loaded(self, tmp_path, pipeline_env, monkeypatch):
        cfg_file = tmp_path / "custom.yaml"
        cfg_file.write_text(yaml.dump({
            "test_command": "make test",
            "overrides": {"audit": {"gate": "soft"}},
        }))
        configs_seen = []
        def mock_run_round(rf, n, total, test_cmd, config, *args, **kwargs):
            configs_seen.append((test_cmd, config))
            return qp.RoundOutcome.PASSED
        monkeypatch.setattr(qp, "run_round", mock_run_round)
        qp.pipeline(
            project_dir=None,
            rounds_arg=None,
            config_file=str(cfg_file),
            start_from=1,
            dry_run=False,
            worktree=False,
            worktree_symlinks=None,
            test_command=None,
            review_flag=None,
            log_dir_arg=str(pipeline_env / "logs"),
        )
        assert configs_seen[0][0] == "make test"
        assert "audit" in configs_seen[0][1].overrides
