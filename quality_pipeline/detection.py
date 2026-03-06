"""Test command detection and static analysis."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .config import DEFAULT_ANALYZERS, MAX_ANALYSIS_OUTPUT

_ANALYZER_TIMEOUT_SECS = "120"


def detect_test_command(project_dir: Path) -> str | None:
    """Auto-detect the test command for a project.

    Priority: CLAUDE.md -> Makefile -> package.json -> pyproject.toml ->
    go.mod -> Cargo.toml
    """
    # 1. CLAUDE.md
    claude_md = project_dir / "CLAUDE.md"
    if claude_md.exists():
        text = claude_md.read_text(errors="replace")
        # Explicit "test command:" or "run tests:" line
        m = re.search(
            r"^\s*(?:test command|run tests):?\s+(.+)",
            text, re.IGNORECASE | re.MULTILINE,
        )
        if m:
            return m.group(1).strip()
        # Backtick-wrapped test commands
        m = re.search(
            r"`((?:pytest|jest|vitest|npm test|yarn test|bun test|pnpm test"
            r"|make test|cargo test|go test)[^`]*)`",
            text,
        )
        if m:
            return m.group(1)

    # 2. Makefile with test target
    makefile = project_dir / "Makefile"
    if makefile.exists():
        try:
            if re.search(r"^test\s*:", makefile.read_text(), re.MULTILINE):
                return "make test"
        except Exception:
            pass

    # 3. package.json with test script
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text())
            test_script = data.get("scripts", {}).get("test", "")
            if test_script and "no test specified" not in test_script:
                # Pick the right package manager
                if (project_dir / "bun.lockb").exists() or (
                    project_dir / "bun.lock"
                ).exists():
                    return "bun test"
                if (project_dir / "pnpm-lock.yaml").exists():
                    return "pnpm test"
                if (project_dir / "yarn.lock").exists():
                    return "yarn test"
                return "npm test"
        except (json.JSONDecodeError, OSError):
            pass

    # 4. Python (pyproject.toml / setup.cfg / tests dir)
    has_uv_lock = (project_dir / "uv.lock").exists()
    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text()
            if re.search(r"\[tool\.pytest", content) or "pytest" in content:
                return "uv run pytest" if has_uv_lock else "pytest"
        except Exception:
            pass

    setup_cfg = project_dir / "setup.cfg"
    if setup_cfg.exists():
        try:
            if "[tool:pytest]" in setup_cfg.read_text():
                return "uv run pytest" if has_uv_lock else "pytest"
        except Exception:
            pass

    if (project_dir / "tests").is_dir() or (project_dir / "test").is_dir():
        python_markers = ["requirements.txt", "pyproject.toml", "setup.py"]
        if any((project_dir / m).exists() for m in python_markers):
            if has_uv_lock:
                return "uv run pytest"
            if shutil.which("pytest"):
                return "pytest"
            return None

    # 5. Go
    if (project_dir / "go.mod").exists():
        return "go test ./..."

    # 6. Rust
    if (project_dir / "Cargo.toml").exists():
        return "cargo test"

    return None


def _run_analyzer(name: str, args: list[str], project_dir: Path,
                  prerequisites: list[str] | None = None) -> str:
    """Run a single analyzer tool if available. Returns output or empty string."""
    if not shutil.which(name):
        return ""
    # Check tool-specific prerequisites
    if prerequisites:
        if not any((project_dir / p).exists() for p in prerequisites):
            return ""
    # Detect timeout command
    timeout_cmd: list[str] = []
    if shutil.which("gtimeout"):
        timeout_cmd = ["gtimeout", _ANALYZER_TIMEOUT_SECS]
    elif shutil.which("timeout"):
        timeout_cmd = ["timeout", _ANALYZER_TIMEOUT_SECS]

    try:
        result = subprocess.run(
            [*timeout_cmd, *args],
            capture_output=True, text=True, check=False, cwd=project_dir,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def run_static_analysis(
    round_name: str, project_dir: Path, analyzers_override: str = ""
) -> str:
    """Run static analysis tools relevant to a round. Returns combined output."""
    analyzers_str = analyzers_override or DEFAULT_ANALYZERS.get(round_name, "")
    if not analyzers_str:
        return ""

    python_prereqs = ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"]

    analyzer_defs: dict[str, tuple[list[str], list[str] | None]] = {
        "bandit": (
            ["bandit", "-r", ".", "-f", "txt", "--severity-filter", "medium"],
            python_prereqs,
        ),
        "semgrep": (
            ["semgrep", "--config", "auto", "--quiet", "--no-git-ignore"],
            None,
        ),
        "mypy": (
            ["mypy", ".", "--no-error-summary"],
            ["pyproject.toml", "setup.py", "setup.cfg", "mypy.ini"],
        ),
        "pyright": (
            ["pyright", "."],
            ["pyproject.toml", "setup.py", "setup.cfg", "pyrightconfig.json"],
        ),
        "tsc": (
            ["tsc", "--noEmit"],
            ["tsconfig.json"],
        ),
        "vulture": (
            ["vulture", "."],
            python_prereqs,
        ),
    }

    output_parts: list[str] = []
    for analyzer in analyzers_str.split():
        defn = analyzer_defs.get(analyzer)
        if not defn:
            continue
        args, prereqs = defn
        result = _run_analyzer(analyzer, args, project_dir, prereqs)
        if result:
            output_parts.append(f"### {analyzer}\n{result}")

    combined = "\n\n".join(output_parts)
    if not combined:
        return ""
    if len(combined) > MAX_ANALYSIS_OUTPUT:
        return combined[:MAX_ANALYSIS_OUTPUT] + "\n[... truncated]"
    return combined
