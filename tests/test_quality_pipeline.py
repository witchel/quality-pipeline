"""Tests for quality_pipeline.py — pure-logic and light-filesystem functions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
