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
            qp.detection, "_run_analyzer",
            lambda name, args, proj, prereqs=None: f"output-{name}",
        )
        result = qp.run_static_analysis("any-round", Path("."), "mypy vulture")
        assert "### mypy" in result
        assert "output-mypy" in result
        assert "### vulture" in result

    def test_truncation(self, monkeypatch):
        monkeypatch.setattr(
            qp.detection, "_run_analyzer",
            lambda name, args, proj, prereqs=None: "x" * 5000,
        )
        result = qp.run_static_analysis("security", Path("."))
        assert result.endswith("\n[... truncated]")
        assert len(result) <= qp.MAX_ANALYSIS_OUTPUT + len("\n[... truncated]")

    def test_unknown_analyzer_skipped(self, monkeypatch):
        monkeypatch.setattr(
            qp.detection, "_run_analyzer",
            lambda name, args, proj, prereqs=None: "found",
        )
        result = qp.run_static_analysis("any", Path("."), "nonexistent_tool")
        assert result == ""
