"""Tests for quality_pipeline.detection — test command detection, static analysis."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import quality_pipeline as qp


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

    def test_setup_cfg_with_uv_lock(self, tmp_path):
        """setup.cfg with [tool:pytest] + uv.lock should use 'uv run pytest'."""
        (tmp_path / "setup.cfg").write_text("[tool:pytest]\n")
        (tmp_path / "uv.lock").write_text("")
        assert qp.detect_test_command(tmp_path) == "uv run pytest"

    def test_test_dir_singular(self, tmp_path):
        """'test/' dir (not just 'tests/') with python markers should detect pytest."""
        (tmp_path / "test").mkdir()
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
        (tmp_path / "uv.lock").write_text("")
        assert qp.detect_test_command(tmp_path) == "uv run pytest"

    def test_claude_md_run_tests_line(self, tmp_path):
        """'run tests:' variant should also be detected."""
        (tmp_path / "CLAUDE.md").write_text("run tests: make check\n")
        assert qp.detect_test_command(tmp_path) == "make check"

    def test_pyproject_with_pytest_mention(self, tmp_path):
        """A pyproject.toml mentioning 'pytest' in dependencies should detect it."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\ndependencies = ["pytest"]\n'
        )
        assert qp.detect_test_command(tmp_path) == "pytest"


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
            lambda *a, **_kw: MagicMock(stdout="  finding1  ", returncode=0),
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
            lambda *a, **_kw: (_ for _ in ()).throw(OSError("boom")),
        )
        assert qp._run_analyzer("mypy", ["mypy", "."], tmp_path) == ""

    def test_timeout_prefix_gtimeout(self, tmp_path, monkeypatch):
        run_calls = []
        def mock_which(name):
            return f"/usr/bin/{name}"
        def mock_run(*args, **_kwargs):
            run_calls.append(args[0])
            return MagicMock(stdout="ok", returncode=0)
        monkeypatch.setattr(shutil, "which", mock_which)
        monkeypatch.setattr(subprocess, "run", mock_run)
        qp._run_analyzer("mypy", ["mypy", "."], tmp_path)
        assert run_calls[0][0] == "gtimeout"

    def test_timeout_prefix_timeout_fallback(self, tmp_path, monkeypatch):
        """When gtimeout is unavailable, should fall back to timeout."""
        run_calls = []
        def mock_which(name):
            if name == "gtimeout":
                return None
            return f"/usr/bin/{name}"
        def mock_run(*args, **_kwargs):
            run_calls.append(args[0])
            return MagicMock(stdout="ok", returncode=0)
        monkeypatch.setattr(shutil, "which", mock_which)
        monkeypatch.setattr(subprocess, "run", mock_run)
        qp._run_analyzer("mypy", ["mypy", "."], tmp_path)
        assert run_calls[0][0] == "timeout"


class TestRunStaticAnalysis:
    def test_unknown_round_no_analyzers(self):
        assert qp.run_static_analysis("unknown-round", Path(".")) == ""

    def test_override_analyzers(self, monkeypatch):
        monkeypatch.setattr(
            qp.detection, "_run_analyzer",
            lambda name, _args, _proj, _prereqs=None: f"output-{name}",
        )
        result = qp.run_static_analysis("any-round", Path("."), "mypy vulture")
        assert "### mypy" in result
        assert "output-mypy" in result
        assert "### vulture" in result

    def test_truncation(self, monkeypatch):
        monkeypatch.setattr(
            qp.detection, "_run_analyzer",
            lambda _name, _args, _proj, _prereqs=None: "x" * 5000,
        )
        result = qp.run_static_analysis("security", Path("."))
        assert result.endswith("\n[... truncated]")
        assert len(result) <= qp.MAX_ANALYSIS_OUTPUT + len("\n[... truncated]")

    def test_unknown_analyzer_skipped(self, monkeypatch):
        monkeypatch.setattr(
            qp.detection, "_run_analyzer",
            lambda _name, _args, _proj, _prereqs=None: "found",
        )
        result = qp.run_static_analysis("any", Path("."), "nonexistent_tool")
        assert result == ""

    def test_default_round_analyzers(self, monkeypatch):
        """Known round name should use DEFAULT_ANALYZERS mapping."""
        called = []
        def mock_analyzer(name, _args, _proj, _prereqs=None):
            called.append(name)
            return f"output-{name}"
        monkeypatch.setattr(qp.detection, "_run_analyzer", mock_analyzer)
        result = qp.run_static_analysis("security", Path("."))
        assert "### bandit" in result
        assert "### semgrep" in result
        assert set(called) == {"bandit", "semgrep", "ruff-security"}

    def test_maintainability_default_analyzers(self, monkeypatch):
        """Maintainability should use both refactor and simplify ruff rules."""
        called = []
        def mock_analyzer(name, _args, _proj, _prereqs=None):
            called.append(name)
            return f"output-{name}"
        monkeypatch.setattr(qp.detection, "_run_analyzer", mock_analyzer)
        result = qp.run_static_analysis("maintainability", Path("."))
        assert "### ruff-refactor" in result
        assert "### ruff-simplify" in result
        assert set(called) == {"ruff-refactor", "ruff-simplify"}

    def test_all_analyzers_empty_output(self, monkeypatch):
        """When all analyzers produce no output, result should be empty."""
        monkeypatch.setattr(
            qp.detection, "_run_analyzer",
            lambda _name, _args, _proj, _prereqs=None: "",
        )
        result = qp.run_static_analysis("security", Path("."))
        assert result == ""

    def test_default_analyzers_include_ruff_and_codegraph(self):
        """DEFAULT_ANALYZERS should include the new ruff and codegraph entries."""
        assert "ruff-security" in qp.DEFAULT_ANALYZERS["security"]
        assert "ruff-dead-code" in qp.DEFAULT_ANALYZERS["dead-code"]
        assert "codegraph-unused" in qp.DEFAULT_ANALYZERS["dead-code"]
        assert "ruff-simplify" in qp.DEFAULT_ANALYZERS["simplify"]
        assert "ruff-refactor" in qp.DEFAULT_ANALYZERS["refactor"]
        assert qp.DEFAULT_ANALYZERS["maintainability"] == (
            "ruff-refactor ruff-simplify"
        )


class TestRunAnalyzerVirtualNames:
    """Tests for _run_analyzer binary detection using args[0], not name."""

    def test_virtual_name_uses_args0_for_which(self, tmp_path, monkeypatch):
        """Binary lookup should use args[0] ('ruff'), not name ('ruff-dead-code')."""
        (tmp_path / "pyproject.toml").write_text("")
        which_calls = []

        def mock_which(name):
            which_calls.append(name)
            if name in ("gtimeout", "timeout"):
                return None
            if name == "ruff":
                return "/usr/bin/ruff"
            return None  # "ruff-dead-code" would not be found

        monkeypatch.setattr(shutil, "which", mock_which)
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **_kw: MagicMock(stdout="found issue", returncode=0),
        )
        result = qp._run_analyzer(
            "ruff-dead-code",
            ["ruff", "check", "--select", "F401", "."],
            tmp_path,
            ["pyproject.toml"],
        )
        # Should succeed because args[0]="ruff" is found
        assert result == "found issue"
        # First which() call should be for "ruff", not "ruff-dead-code"
        assert which_calls[0] == "ruff"

    def test_new_analyzer_defs_have_correct_binaries(self):
        """Each new analyzer entry should exist in analyzer_defs with the right binary."""
        # Call run_static_analysis to exercise the defs; we just need to inspect them.
        # Instead, import and inspect the function source indirectly:
        # We verify by calling with overrides and checking the args passed.
        expected = {
            "ruff-dead-code": "ruff",
            "ruff-simplify": "ruff",
            "ruff-security": "ruff",
            "ruff-refactor": "ruff",
            "codegraph-unused": "codegraph",
        }
        captured_args = {}

        def mock_analyzer(name, args, _proj, _prereqs=None):
            captured_args[name] = args
            return ""

        import quality_pipeline.detection as det
        orig = det._run_analyzer
        det._run_analyzer = mock_analyzer
        try:
            for analyzer_name in expected:
                qp.run_static_analysis("any", Path("."), analyzer_name)
        finally:
            det._run_analyzer = orig

        for analyzer_name, expected_binary in expected.items():
            assert analyzer_name in captured_args, (
                f"{analyzer_name} not found in analyzer_defs"
            )
            assert captured_args[analyzer_name][0] == expected_binary, (
                f"{analyzer_name} binary should be {expected_binary}, "
                f"got {captured_args[analyzer_name][0]}"
            )
