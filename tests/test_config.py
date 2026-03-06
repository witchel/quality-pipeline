"""Tests for quality_pipeline.config — frontmatter, overrides, round discovery."""

from __future__ import annotations

import yaml

import quality_pipeline as qp


class TestParseFrontmatter:
    def test_valid_frontmatter(self, tmp_path):
        f = tmp_path / "round.md"
        f.write_text(
            "---\n"
            "name: audit-tests\n"
            "gate: soft\n"
            "max_budget_usd: 3.50\n"
            "max_turns: 15\n"
            "max_time_minutes: 20\n"
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
        assert rc.max_time_minutes == 20
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
        assert rc.max_budget_usd is None
        assert rc.max_time_minutes is None

    def test_defaults(self, tmp_path):
        """Unset fields are None; finalization fills in defaults."""
        f = tmp_path / "round.md"
        f.write_text("---\nname: minimal\n---\nPrompt here.\n")
        rc = qp.parse_frontmatter(f)
        assert rc.name == "minimal"
        assert rc.gate == "hard"
        assert rc.max_budget_usd is None
        assert rc.max_turns is None
        assert rc.max_time_minutes is None
        assert rc.max_retries == 0
        assert rc.review is None
        assert rc.analyzers == ""
        assert rc.commit_message_prefix == "chore: "

    def test_invalid_yaml(self, tmp_path):
        f = tmp_path / "round.md"
        f.write_text("---\n: [invalid yaml\n---\nPrompt.\n")
        rc = qp.parse_frontmatter(f)
        assert rc.name == ""

    def test_invalid_yaml_warns(self, tmp_path, capsys):
        """Invalid YAML frontmatter should emit a warning."""
        f = tmp_path / "round.md"
        f.write_text("---\n: [invalid yaml\n---\nPrompt.\n")
        qp.parse_frontmatter(f)
        captured = capsys.readouterr()
        assert "Failed to parse frontmatter" in captured.out

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
                "max_time_minutes": 25,
                "gate": "none",
                "max_retries": 5,
                "review": True,
                "analyzers": "mypy pyright",
            }
        })
        result = qp.apply_config_overrides(rc, cfg)
        assert result.max_budget_usd == 10.0
        assert result.max_time_minutes == 25
        assert result.gate == "none"
        assert result.max_retries == 5
        assert result.review is True
        assert result.analyzers == "mypy pyright"

    def test_global_budget_applied(self):
        rc = qp.RoundConfig(name="test")
        cfg = qp.PipelineConfig(max_budget_usd=10.0)
        result = qp.apply_config_overrides(rc, cfg)
        assert result.max_budget_usd == 10.0

    def test_global_time_applied(self):
        rc = qp.RoundConfig(name="test")
        cfg = qp.PipelineConfig(max_time_minutes=25)
        result = qp.apply_config_overrides(rc, cfg)
        assert result.max_time_minutes == 25

    def test_per_round_override_beats_global(self):
        rc = qp.RoundConfig(name="test")
        cfg = qp.PipelineConfig(
            max_time_minutes=25,
            overrides={"test": {"max_time_minutes": 30}},
        )
        result = qp.apply_config_overrides(rc, cfg)
        assert result.max_time_minutes == 30

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

    def test_frontmatter_explicit_beats_global(self):
        """Frontmatter-set value should not be overwritten by global config."""
        rc = qp.RoundConfig(name="test", max_budget_usd=3.0)
        cfg = qp.PipelineConfig(max_budget_usd=10.0)
        result = qp.apply_config_overrides(rc, cfg)
        assert result.max_budget_usd == 3.0


class TestFinalizeRoundConfig:
    def test_fills_defaults(self):
        rc = qp.RoundConfig(name="test")
        result = qp._finalize_round_config(rc)
        assert result.max_budget_usd == qp._DEFAULT_MAX_BUDGET_USD
        assert result.max_turns == qp._DEFAULT_MAX_TURNS
        assert result.max_time_minutes == qp._DEFAULT_MAX_TIME_MINUTES

    def test_preserves_explicit_values(self):
        rc = qp.RoundConfig(
            name="test", max_budget_usd=3.0, max_turns=10, max_time_minutes=5
        )
        result = qp._finalize_round_config(rc)
        assert result.max_budget_usd == 3.0
        assert result.max_turns == 10
        assert result.max_time_minutes == 5

    def test_invalid_gate_warns_and_defaults(self, capsys):
        rc = qp.RoundConfig(name="test", gate="hardd")
        result = qp._finalize_round_config(rc)
        assert result.gate == "hard"
        captured = capsys.readouterr()
        assert "Unknown gate" in captured.out

    def test_valid_gates_unchanged(self):
        for gate in ("hard", "soft", "none"):
            rc = qp.RoundConfig(name="test", gate=gate)
            result = qp._finalize_round_config(rc)
            assert result.gate == gate

    def test_review_none_resolved_to_false(self):
        """review=None should be finalized to False."""
        rc = qp.RoundConfig(name="test", review=None)
        result = qp._finalize_round_config(rc)
        assert result.review is False

    def test_review_true_preserved(self):
        rc = qp.RoundConfig(name="test", review=True)
        result = qp._finalize_round_config(rc)
        assert result.review is True


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

    def test_max_budget_coerced_to_float(self, tmp_path):
        """YAML integer max_budget_usd should be coerced to float."""
        f = tmp_path / "pipeline.yaml"
        f.write_text(yaml.dump({"max_budget_usd": 5}))
        cfg = qp.load_pipeline_config(f)
        assert isinstance(cfg.max_budget_usd, float)
        assert cfg.max_budget_usd == 5.0


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


class TestDiscoverRounds:
    def test_finds_md_files_sorted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp.config, "ROUNDS_DIR", tmp_path)
        (tmp_path / "02-second.md").write_text("---\nname: second\n---\n")
        (tmp_path / "01-first.md").write_text("---\nname: first\n---\n")
        (tmp_path / "readme.txt").write_text("not a round")
        result = qp.discover_rounds()
        assert len(result) == 2
        assert result[0].name == "01-first.md"
        assert result[1].name == "02-second.md"

    def test_empty_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp.config, "ROUNDS_DIR", tmp_path)
        assert qp.discover_rounds() == []

    def test_missing_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp.config, "ROUNDS_DIR", tmp_path / "nonexistent")
        assert qp.discover_rounds() == []


class TestResolveRoundFile:
    def test_match_by_frontmatter_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp.config, "ROUNDS_DIR", tmp_path)
        f = tmp_path / "01-audit.md"
        f.write_text("---\nname: audit-tests\n---\nDo audit.\n")
        assert qp.resolve_round_file("audit-tests") == f

    def test_match_by_filename_dash_pattern(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp.config, "ROUNDS_DIR", tmp_path)
        f = tmp_path / "01-security.md"
        f.write_text("---\nname: something-else\n---\n")
        assert qp.resolve_round_file("security") == f

    def test_match_by_filename_glob(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp.config, "ROUNDS_DIR", tmp_path)
        f = tmp_path / "refactor-code.md"
        f.write_text("---\nname: other\n---\n")
        assert qp.resolve_round_file("refactor") == f

    def test_no_match(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qp.config, "ROUNDS_DIR", tmp_path)
        (tmp_path / "01-audit.md").write_text("---\nname: audit\n---\n")
        assert qp.resolve_round_file("nonexistent") is None
