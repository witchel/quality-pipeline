#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "click>=8.0",
#     "pyyaml>=6.0",
# ]
# ///
"""quality_pipeline.py — Multi-round automated code quality pipeline.

Orchestrates sequential `claude -p` invocations, each with a focused objective,
test verification, and a clean git commit. Replaces the former shell scripts
(quality-pipeline.sh, detect-test-command.sh, run-static-analysis.sh).
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

import click
import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = SCRIPT_DIR.parent
ROUNDS_DIR = PLUGIN_DIR / "rounds"
TEMPLATE_DIR = PLUGIN_DIR / "templates"

DEFAULT_SYMLINK_DIRS: list[str] = [
    "node_modules", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    ".next", ".nuxt", "vendor", ".bundle",
]

ENV_FILES = [".env", ".env.local", ".env.test"]

BRANCH_PREFIX_DEFAULT = "quality"

DEFAULT_ANALYZERS: dict[str, str] = {
    "security": "bandit semgrep",
    "type-safety": "mypy pyright tsc",
    "dead-code": "vulture",
}

MAX_ANALYSIS_OUTPUT = 4000

VALID_GATES = {"hard", "soft", "none"}

_DEFAULT_MAX_BUDGET_USD = 5.00
_DEFAULT_MAX_TURNS = 30
_DEFAULT_MAX_TIME_MINUTES = 15

# ---------------------------------------------------------------------------
# Dataclasses & enums
# ---------------------------------------------------------------------------


@dataclass
class RoundConfig:
    name: str = ""
    commit_message_prefix: str = "chore: "
    max_budget_usd: float | None = None
    max_turns: int | None = None
    max_time_minutes: int | None = None
    gate: str = "hard"
    max_retries: int = 0
    review: bool | None = None  # None = not set (defaults to False)
    analyzers: str = ""


@dataclass
class PipelineConfig:
    test_command: str = ""
    rounds: list[str] = field(default_factory=list)
    branch_prefix: str = ""
    max_budget_usd: float | None = None
    max_time_minutes: int | None = None
    overrides: dict[str, dict] = field(default_factory=dict)


class RoundOutcome(Enum):
    PASSED = "passed"
    NO_CHANGES = "no-changes"
    HARD_FAILED = "HARD-FAILED"
    SOFT_FAILED = "soft-failed"
    SKIPPED = "skipped"


@dataclass
class RoundResult:
    name: str
    outcome: RoundOutcome


# ---------------------------------------------------------------------------
# Color output
# ---------------------------------------------------------------------------


class ColorOutput:
    def __init__(self) -> None:
        is_tty = sys.stdout.isatty()
        self.RED = "\033[0;31m" if is_tty else ""
        self.GREEN = "\033[0;32m" if is_tty else ""
        self.YELLOW = "\033[1;33m" if is_tty else ""
        self.BLUE = "\033[0;34m" if is_tty else ""
        self.BOLD = "\033[1m" if is_tty else ""
        self.NC = "\033[0m" if is_tty else ""

    def log(self, msg: str) -> None:
        print(f"{self.BLUE}[pipeline]{self.NC} {msg}", flush=True)

    def ok(self, msg: str) -> None:
        print(f"{self.GREEN}[pipeline]{self.NC} {msg}", flush=True)

    def warn(self, msg: str) -> None:
        print(f"{self.YELLOW}[pipeline]{self.NC} {msg}", flush=True)

    def err(self, msg: str) -> None:
        print(f"{self.RED}[pipeline]{self.NC} {msg}", file=sys.stderr, flush=True)


C = ColorOutput()

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def gate_label(gate: str) -> str:
    labels = {
        "hard": f"{C.RED}HARD{C.NC}",
        "soft": f"{C.YELLOW}SOFT{C.NC}",
        "none": f"{C.BLUE}NONE{C.NC}",
    }
    return labels.get(gate, gate)


def format_duration(secs: int) -> str:
    if secs >= 3600:
        return f"{secs // 3600}h {secs % 3600 // 60}m {secs % 60}s"
    if secs >= 60:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs}s"


def git(*args: str, capture: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command, returning CompletedProcess."""
    return subprocess.run(
        ["git", *args],
        capture_output=capture,
        text=True,
        check=check,
    )


# ---------------------------------------------------------------------------
# Resource monitoring
# ---------------------------------------------------------------------------


def detect_gpu() -> str:
    if shutil.which("nvidia-smi"):
        try:
            subprocess.run(
                ["nvidia-smi"], capture_output=True, check=True, timeout=5
            )
            return "nvidia"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    if shutil.which("rocm-smi"):
        return "rocm"
    return "none"


def get_resource_snapshot(gpu_type: str = "none") -> str:
    # CPU
    try:
        load1 = os.getloadavg()[0]
        ncpu = os.cpu_count() or "?"
        cpu_info = f"load {load1:.1f} ({ncpu} cores)"
    except OSError:
        cpu_info = "?"

    # Memory
    mem_info = "?"
    system = platform.system()
    if system == "Darwin":
        try:
            mem_total = int(
                subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
            ) // (1024 * 1024)
            page_size = int(
                subprocess.run(
                    ["sysctl", "-n", "hw.pagesize"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
            )
            vm_out = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, check=True
            ).stdout
            pages = {"active": 0, "wired": 0, "compressed": 0}
            for line in vm_out.splitlines():
                if "Pages active" in line:
                    pages["active"] = int(re.sub(r"\D", "", line.split(":")[-1]))
                elif "Pages wired" in line:
                    pages["wired"] = int(re.sub(r"\D", "", line.split(":")[-1]))
                elif "occupied by compressor" in line:
                    pages["compressed"] = int(re.sub(r"\D", "", line.split(":")[-1]))
            used_mb = sum(pages.values()) * page_size // (1024 * 1024)
            if mem_total > 0:
                pct = used_mb * 100 // mem_total
                mem_info = f"{used_mb}/{mem_total} MB ({pct}%)"
        except Exception:
            pass
    elif system == "Linux":
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            try:
                data = meminfo.read_text()
                total = avail = 0
                for line in data.splitlines():
                    if line.startswith("MemTotal:"):
                        total = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail = int(line.split()[1])
                if total > 0:
                    used = total - avail
                    mem_info = (
                        f"{used // 1024}/{total // 1024} MB "
                        f"({used * 100 // total}%)"
                    )
            except Exception:
                pass

    # GPU
    gpu_info = ""
    if gpu_type == "nvidia":
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, check=True, timeout=5,
            ).stdout.strip()
            parts = []
            any_active = False
            for line in out.splitlines():
                fields = [f.strip() for f in line.split(",")]
                if len(fields) >= 4:
                    idx, util, mem_u, mem_t = fields[:4]
                    if util.isdigit() and int(util) > 0:
                        any_active = True
                    parts.append(f"GPU{idx}: {util}% VRAM {mem_u}/{mem_t} MB")
            if any_active:
                gpu_info = ", ".join(parts)
        except Exception:
            pass
    elif gpu_type == "rocm":
        try:
            out = subprocess.run(
                ["rocm-smi", "--showgpuuse"],
                capture_output=True, text=True, check=True, timeout=5,
            ).stdout
            m = re.search(r"(\d+)\s*%", out)
            if m and int(m.group(1)) > 0:
                gpu_info = f"GPU: {m.group(1)}%"
        except Exception:
            pass

    report = f"CPU: {cpu_info} | Mem: {mem_info}"
    if gpu_info:
        report += f" | {gpu_info}"
    return report


class ResourceMonitor:
    """Daemon thread that logs resource usage periodically."""

    def __init__(
        self, interval: int, gpu_type: str, round_name: str, start_epoch: float
    ) -> None:
        self._stop = threading.Event()
        self._interval = interval
        self._gpu_type = gpu_type
        self._round_name = round_name
        self._start_epoch = start_epoch
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            elapsed = int(time.time() - self._start_epoch)
            snapshot = get_resource_snapshot(self._gpu_type)
            C.log(f"  \u23f1 {format_duration(elapsed)} | {snapshot}")


# ---------------------------------------------------------------------------
# Cleanup manager
# ---------------------------------------------------------------------------


class PipelineCleanup:
    """Tracks resources for atexit cleanup."""

    def __init__(self) -> None:
        self.temp_files: list[Path] = []
        self.worktree_dir: Path | None = None
        self.original_dir: Path | None = None
        self.symlink_dirs: list[str] = []
        self.lock_dir: Path | None = None
        self.monitor: ResourceMonitor | None = None
        self.current_round: str = ""
        self.worktree_mode: bool = False
        atexit.register(self.cleanup)

    def make_temp(self) -> Path:
        fd, path = tempfile.mkstemp()
        os.close(fd)
        p = Path(path)
        self.temp_files.append(p)
        return p

    def cleanup(self) -> None:
        # Block signals so cleanup can't be interrupted mid-way
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)

        if self.monitor:
            self.monitor.stop()
        for f in self.temp_files:
            f.unlink(missing_ok=True)
        self._cleanup_worktree()
        if self.lock_dir and self.lock_dir.exists():
            try:
                self.lock_dir.rmdir()
            except OSError:
                pass
        if self.current_round:
            print()
            C.err(f"Interrupted during round: {self.current_round}")
            if self.worktree_mode:
                C.warn("Worktree removed. Original repo is unchanged.")
            else:
                C.warn("Partial changes may remain. Clean up with:")
                C.warn("  git reset --hard && git clean -fd")

    def _cleanup_worktree(self) -> None:
        if not self.worktree_dir:
            return
        wt = self.worktree_dir
        self.worktree_dir = None

        if self.original_dir:
            os.chdir(self.original_dir)

        # Remove symlinks first
        for d in self.symlink_dirs:
            link = wt / d
            if link.is_symlink():
                link.unlink()
        for ef in ENV_FILES:
            link = wt / ef
            if link.is_symlink():
                link.unlink()

        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt)],
                capture_output=True, check=True,
            )
            C.ok(f"Removed worktree: {wt}")
        except subprocess.CalledProcessError:
            C.warn("git worktree remove failed — cleaning up manually")
            shutil.rmtree(wt, ignore_errors=True)
            subprocess.run(
                ["git", "worktree", "prune"], capture_output=True, check=False
            )


# Global cleanup manager
_cleanup = PipelineCleanup()


def _handle_signal(signum: int, _frame: object) -> None:  # noqa: ARG001
    sys.exit(128 + signum)


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)
if hasattr(signal, "SIGHUP"):
    signal.signal(signal.SIGHUP, _handle_signal)


# ---------------------------------------------------------------------------
# Test command detection
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Static analysis
# ---------------------------------------------------------------------------


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
        timeout_cmd = ["gtimeout", "120"]
    elif shutil.which("timeout"):
        timeout_cmd = ["timeout", "120"]

    try:
        result = subprocess.run(
            [*timeout_cmd, *args],
            capture_output=True, text=True, check=False, cwd=project_dir,
        )
        return result.stdout.strip()
    except Exception:
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


# ---------------------------------------------------------------------------
# Frontmatter & config
# ---------------------------------------------------------------------------


def _parse_review_bool(val: object) -> bool | None:
    """Convert a review field value to bool | None."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() == "true"
    return None


def parse_frontmatter(path: Path) -> RoundConfig:
    """Parse YAML frontmatter from a round file into RoundConfig."""
    text = path.read_text()
    # Extract content between first and second ---
    parts = text.split("---", 2)
    if len(parts) < 3:
        return RoundConfig()

    try:
        data = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return RoundConfig()

    review = _parse_review_bool(data.get("review"))

    return RoundConfig(
        name=str(data.get("name", "")),
        commit_message_prefix=str(data.get("commit_message_prefix", "chore: ")),
        max_budget_usd=float(data["max_budget_usd"]) if "max_budget_usd" in data else None,
        max_turns=int(data["max_turns"]) if "max_turns" in data else None,
        max_time_minutes=int(data["max_time_minutes"]) if "max_time_minutes" in data else None,
        gate=str(data.get("gate", "hard")),
        max_retries=int(data.get("max_retries", 0)),
        review=review,
        analyzers=str(data.get("analyzers", "")),
    )


def get_round_prompt(path: Path) -> str:
    """Extract prompt body (everything after the closing --- of frontmatter)."""
    text = path.read_text()
    parts = text.split("---", 2)
    if len(parts) >= 3:
        return parts[2].lstrip("\n")
    return text


def load_pipeline_config(path: Path) -> PipelineConfig:
    """Load pipeline.yaml configuration."""
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (yaml.YAMLError, OSError) as e:
        C.warn(f"Failed to parse config: {e}")
        return PipelineConfig()

    overrides = {}
    for name, ov in data.get("overrides", {}).items():
        if isinstance(ov, dict):
            overrides[name] = ov

    raw_time = data.get("max_time_minutes")
    return PipelineConfig(
        test_command=str(data.get("test_command", "")),
        rounds=list(data.get("rounds", [])),
        branch_prefix=str(data.get("branch_prefix", "")),
        max_budget_usd=data.get("max_budget_usd"),
        max_time_minutes=int(raw_time) if raw_time is not None else None,
        overrides=overrides,
    )


def _find_override(name: str, config: PipelineConfig) -> dict:
    """Look up per-round override dict, normalizing dashes/underscores."""
    ov = config.overrides.get(name)
    if ov is not None:
        return ov
    normalized = name.replace("-", "_")
    for key, val in config.overrides.items():
        if key.replace("-", "_") == normalized:
            return val
    return {}


def _finalize_round_config(rc: RoundConfig) -> RoundConfig:
    """Fill in defaults for unset fields and validate gate value."""
    changes: dict[str, object] = {}
    if rc.max_budget_usd is None:
        changes["max_budget_usd"] = _DEFAULT_MAX_BUDGET_USD
    if rc.max_turns is None:
        changes["max_turns"] = _DEFAULT_MAX_TURNS
    if rc.max_time_minutes is None:
        changes["max_time_minutes"] = _DEFAULT_MAX_TIME_MINUTES
    if rc.gate not in VALID_GATES:
        C.warn(f"Unknown gate '{rc.gate}' for round '{rc.name}' — defaulting to 'hard'")
        changes["gate"] = "hard"
    return replace(rc, **changes) if changes else rc


def apply_config_overrides(rc: RoundConfig, config: PipelineConfig) -> RoundConfig:
    """Apply per-round overrides then global defaults, returning a finalized RoundConfig.

    Priority (highest first): per-round override > frontmatter > global config > default.
    """
    ov = _find_override(rc.name, config)

    # Nothing to apply — no globals set and no per-round overrides
    if not ov and config.max_budget_usd is None and config.max_time_minutes is None:
        return _finalize_round_config(rc)

    rc = replace(rc)

    # Per-round overrides (highest priority), then global config for unset fields
    if "max_budget_usd" in ov:
        rc.max_budget_usd = float(ov["max_budget_usd"])
    elif rc.max_budget_usd is None and config.max_budget_usd is not None:
        rc.max_budget_usd = config.max_budget_usd

    if "max_time_minutes" in ov:
        rc.max_time_minutes = int(ov["max_time_minutes"])
    elif rc.max_time_minutes is None and config.max_time_minutes is not None:
        rc.max_time_minutes = config.max_time_minutes

    if "gate" in ov:
        rc.gate = str(ov["gate"])
    if "max_retries" in ov:
        rc.max_retries = int(ov["max_retries"])
    if "review" in ov:
        rc.review = _parse_review_bool(ov["review"])
    if "analyzers" in ov:
        rc.analyzers = str(ov["analyzers"])
    return _finalize_round_config(rc)


def apply_config_prompt_append(rc_name: str, prompt: str, config: PipelineConfig) -> str:
    """Append config override prompt text if present."""
    ov = _find_override(rc_name, config)
    if ov and ov.get("append_prompt"):
        prompt += "\n\n" + ov["append_prompt"]
    return prompt


# ---------------------------------------------------------------------------
# Round discovery
# ---------------------------------------------------------------------------


def discover_rounds() -> list[Path]:
    """Find all round files sorted by filename."""
    if not ROUNDS_DIR.is_dir():
        return []
    return sorted(f for f in ROUNDS_DIR.glob("*.md") if f.is_file())


def resolve_round_file(name: str) -> Path | None:
    """Resolve a round name to its file path."""
    for f in ROUNDS_DIR.glob("*.md"):
        if not f.is_file():
            continue
        rc = parse_frontmatter(f)
        if rc.name == name:
            return f

    # Try filename pattern matching
    for pattern in [f"*-{name}.md", f"*{name}*.md"]:
        matches = list(ROUNDS_DIR.glob(pattern))
        if matches:
            return matches[0]
    return None


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------


def git_rev_parse_head() -> str:
    return git("rev-parse", "HEAD").stdout.strip()


def git_has_uncommitted() -> bool:
    """Check if tracked files have uncommitted changes."""
    r1 = git("diff", "--quiet", check=False)
    r2 = git("diff", "--cached", "--quiet", check=False)
    return r1.returncode != 0 or r2.returncode != 0


def git_untracked_files() -> set[str]:
    result = git("ls-files", "--others", "--exclude-standard")
    output = result.stdout.strip()
    return set(output.splitlines()) if output else set()


def git_stage_round_changes(pre_untracked: set[str]) -> None:
    """Stage modifications and newly created files (not pre-existing untracked)."""
    git("add", "-u")
    current_untracked = git_untracked_files()
    new_files = current_untracked - pre_untracked
    for f in new_files:
        git("add", "--", f)


def git_rollback_round(pre_untracked: set[str]) -> None:
    """Roll back all changes from a round."""
    git("reset", "HEAD", "--", ".", check=False)
    git("checkout", "--", ".", check=False)
    # Remove only files that appeared during this round
    current_untracked = git_untracked_files()
    new_files = current_untracked - pre_untracked
    for f in new_files:
        Path(f).unlink(missing_ok=True)


def git_create_branch(branch_name: str) -> None:
    """Create or switch to a branch."""
    result = git(
        "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}", check=False
    )
    if result.returncode == 0:
        C.log(f"Branch {branch_name} already exists — using it")
        git("checkout", branch_name)
    else:
        git("checkout", "-b", branch_name)
        C.ok(f"Created branch: {branch_name}")


def git_commit(msg: str) -> None:
    """Commit staged changes."""
    result = git("commit", "-m", msg, "--no-gpg-sign", check=False)
    if result.returncode != 0:
        stderr = result.stderr or ""
        if "gpg" in stderr.lower() or "sign" in stderr.lower():
            git("commit", "-m", msg)
        else:
            C.err(f"git commit failed: {stderr.strip()}")
            raise subprocess.CalledProcessError(result.returncode, "git commit")


def git_acquire_lock(dry_run: bool) -> Path | None:
    """Acquire a lock to prevent concurrent pipeline runs."""
    if dry_run:
        return None
    git_dir = git("rev-parse", "--git-dir").stdout.strip()
    lock_path = Path(git_dir) / "quality-pipeline.lock"
    try:
        lock_path.mkdir()
        return lock_path
    except FileExistsError:
        C.err("Another pipeline is running in this repository.")
        C.err(f"If stale, remove: rmdir '{lock_path}'")
        sys.exit(1)


def setup_worktree(
    branch_name: str, symlink_dirs: list[str]
) -> tuple[Path, Path]:
    """Create an isolated git worktree. Returns (worktree_dir, original_dir)."""
    original_dir = Path.cwd()
    wt_dir = Path(
        tempfile.mkdtemp(prefix="quality-worktree-")
    )
    # git worktree add needs a non-existing target
    wt_dir.rmdir()

    C.log(f"Creating worktree at {wt_dir} on branch {branch_name} ...")
    result = git(
        "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}", check=False
    )
    if result.returncode == 0:
        git("worktree", "add", str(wt_dir), branch_name)
    else:
        git("worktree", "add", "-b", branch_name, str(wt_dir))

    # Symlink dependency directories
    for d in symlink_dirs:
        src = original_dir / d
        dst = wt_dir / d
        if src.is_dir() and not dst.exists():
            dst.symlink_to(src)
            C.log(f"  Symlinked {d}")

    for ef in ENV_FILES:
        src = original_dir / ef
        dst = wt_dir / ef
        if src.is_file() and not dst.exists():
            dst.symlink_to(src)
            C.log(f"  Symlinked {ef}")

    os.chdir(wt_dir)
    C.ok(f"Working in worktree: {wt_dir}")
    return wt_dir, original_dir


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------


def run_tests_with_tee(cmd: str, output_file: Path) -> int:
    """Run tests, teeing output to both stdout and a file. Returns exit code."""
    proc = subprocess.Popen(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    try:
        with output_file.open("w") as fout:
            if proc.stdout is not None:
                for line in proc.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    fout.write(line)
        return proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise


def _claude_env() -> dict[str, str]:
    """Build an environment for spawning claude -p without recursive-run detection."""
    env = os.environ.copy()
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(var, None)
    return env


def run_claude(
    prompt: str,
    system_ctx: str,
    budget: float,
    turns: int,
    log_file: Path,
    timeout_minutes: int = 0,
) -> int:
    """Invoke claude -p and capture output to log. Returns exit code.

    Stderr (progress messages) is tee'd to the terminal so the user can see
    activity.  Stdout (JSON result) is captured only to the log file.

    If *timeout_minutes* > 0, the subprocess is killed after that many minutes
    and a non-zero exit code (``-1``) is returned.
    """
    cmd = [
        "claude", "-p", prompt,
        "--append-system-prompt", system_ctx,
        "--dangerously-skip-permissions",
        "--max-budget-usd", f"{budget:.2f}",
        "--max-turns", str(turns),
        "--output-format", "json",
    ]
    env = _claude_env()
    timeout_secs = timeout_minutes * 60 if timeout_minutes > 0 else None
    timed_out = False

    def _kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        proc.kill()

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env,
    )
    timer: threading.Timer | None = None
    if timeout_secs is not None:
        timer = threading.Timer(timeout_secs, _kill_on_timeout)
        timer.start()
    try:
        with log_file.open("w") as fout:
            # Tee stderr (progress) to terminal + log; capture stdout (JSON) to log only
            def _tee_stderr() -> None:
                assert proc.stderr is not None
                for line in proc.stderr:
                    sys.stderr.write(line)
                    sys.stderr.flush()
                    fout.write(line)

            t = threading.Thread(target=_tee_stderr, daemon=True)
            t.start()

            if proc.stdout is not None:
                for line in proc.stdout:
                    fout.write(line)

            t.join()
            proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        if timer is not None:
            timer.cancel()

    if timed_out:
        C.err(
            f"Claude timed out after {timeout_minutes} minutes — "
            f"increase max_time_minutes if the round needs more time"
        )
        return -1
    return proc.returncode


def _parse_verdict(raw: str) -> str:
    """Extract verdict string from reviewer output (possibly JSON-wrapped)."""
    # Unwrap claude JSON output wrapper: {"result": "..."}
    try:
        outer = json.loads(raw)
        if isinstance(outer, dict) and "result" in outer:
            raw = outer["result"]
    except (json.JSONDecodeError, ValueError):
        pass

    text = raw.strip()
    if text.startswith("```"):
        lines = [line for line in text.split("\n") if not line.startswith("```")]
        text = "\n".join(lines)

    try:
        d = json.loads(text)
        return d.get("verdict", "unknown")
    except (json.JSONDecodeError, ValueError):
        return "unknown"


def run_reviewer(
    round_num: int,
    rc: RoundConfig,
    pre_sha: str,
    log_dir: Path,
    review_flag: bool | None,
) -> None:
    """Run the reviewer pass if enabled."""
    # CLI flag > config/frontmatter (already merged by apply_config_overrides)
    review_enabled = review_flag if review_flag is not None else rc.review
    if not review_enabled:
        return

    C.log("Running reviewer pass...")

    # Get diff (check=False means git() won't raise on non-zero exit)
    diff_result = git("diff", pre_sha, "HEAD", check=False)
    diff_raw = diff_result.stdout or ""
    max_diff = 8000
    if len(diff_raw) > max_diff:
        diff_content = diff_raw[:max_diff] + (
            f"\n\n[... truncated {len(diff_raw) - max_diff} chars — "
            f"review may miss issues in the remainder ...]"
        )
    else:
        diff_content = diff_raw

    if not diff_content:
        C.warn("No diff to review — skipping reviewer")
        return

    template_file = TEMPLATE_DIR / "reviewer.md"
    if not template_file.exists():
        C.warn(f"Reviewer template not found: {template_file} — skipping")
        return

    review_prompt = template_file.read_text().replace("DIFF_PLACEHOLDER", diff_content)
    review_output = log_dir / f"review-round-{round_num}.json"

    # Invoke claude for review (tee stderr to terminal for progress)
    cmd = [
        "claude", "-p", review_prompt,
        "--dangerously-skip-permissions",
        "--max-budget-usd", "1.00",
        "--max-turns", "5",
        "--output-format", "json",
    ]
    rev_proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=_claude_env(),
    )
    try:
        with review_output.open("w") as fout:
            def _tee_rev_stderr() -> None:
                assert rev_proc.stderr is not None
                for line in rev_proc.stderr:
                    sys.stderr.write(line)
                    sys.stderr.flush()

            rt = threading.Thread(target=_tee_rev_stderr, daemon=True)
            rt.start()
            if rev_proc.stdout is not None:
                for line in rev_proc.stdout:
                    fout.write(line)
            rt.join()
            rev_proc.wait()
    except BaseException:
        rev_proc.kill()
        rev_proc.wait()
        raise
    C.log(f"Reviewer claude finished (exit {rev_proc.returncode})")

    verdict = _parse_verdict(review_output.read_text())

    if verdict == "pass":
        C.ok(f"Reviewer: {C.GREEN}PASS{C.NC}")
    elif verdict == "warn":
        C.warn(f"Reviewer: {C.YELLOW}WARN{C.NC} — see {review_output}")
    elif verdict == "critical":
        C.err(f"Reviewer: {C.RED}CRITICAL{C.NC} — see {review_output}")
    else:
        C.warn(f"Reviewer: could not parse verdict — see {review_output}")


def _print_round_header(round_num: int, total: int, rc: RoundConfig) -> None:
    """Print the standard round header banner."""
    print()
    C.log("\u2501" * 60)
    C.log(f"{C.BOLD}Round {round_num}/{total}: {rc.name}{C.NC}")
    C.log(
        f"Budget: ${rc.max_budget_usd:.2f} | Turns: {rc.max_turns} | "
        f"Time: {rc.max_time_minutes}m | "
        f"Gate: {gate_label(rc.gate)} | Retries: {rc.max_retries}"
    )
    C.log("\u2501" * 60)


def run_round(
    round_file: Path,
    round_num: int,
    total_rounds: int,
    test_cmd: str,
    config: PipelineConfig,
    review_flag: bool | None,
    log_dir: Path,
    gpu_type: str,
) -> RoundOutcome:
    """Execute a single pipeline round. Returns the outcome."""
    round_start = time.time()
    pre_sha = git_rev_parse_head()

    rc = parse_frontmatter(round_file)
    rc = apply_config_overrides(rc, config)
    _cleanup.current_round = rc.name
    try:
        return _execute_round(
            round_file, round_num, total_rounds, test_cmd, config,
            review_flag, log_dir, gpu_type, rc, round_start, pre_sha,
        )
    finally:
        _cleanup.current_round = ""


def _execute_round(
    round_file: Path,
    round_num: int,
    total_rounds: int,
    test_cmd: str,
    config: PipelineConfig,
    review_flag: bool | None,
    log_dir: Path,
    gpu_type: str,
    rc: RoundConfig,
    round_start: float,
    pre_sha: str,
) -> RoundOutcome:
    """Inner round execution: prompt, Claude, tests, commit."""
    prompt = get_round_prompt(round_file)
    prompt = apply_config_prompt_append(rc.name, prompt, config)

    _print_round_header(round_num, total_rounds, rc)

    # Build system context
    system_context = (
        f"You are running as part of an automated quality pipeline.\n"
        f"This is round {round_num} of {total_rounds}.\n"
        f"The test command for this project is: {test_cmd}\n"
        f"After making changes, run the tests to verify nothing is broken.\n"
        f"Do not commit your changes — the pipeline handles commits.\n"
        f"Focus exclusively on the task described in the prompt. "
        f"Do not do work that belongs to other rounds.\n"
        f"If the prompt includes a Behavior Contract section, you MUST follow it "
        f"strictly. Items under MUST change are required fixes. Items under MUST NOT "
        f"change are hard constraints."
    )

    # Run static analysis
    analysis_output = run_static_analysis(
        rc.name, Path.cwd(), rc.analyzers
    )
    if analysis_output:
        prompt += (
            "\n\n## Static Analysis Results\n"
            "The following issues were found by static analysis tools. "
            "Use these as a starting point:\n" + analysis_output
        )

    # Snapshot untracked files before this round
    pre_untracked = git_untracked_files()

    # Log initial resource state and start monitor
    C.log(f"Resources: {get_resource_snapshot(gpu_type)}")
    monitor = ResourceMonitor(60, gpu_type, rc.name, round_start)
    monitor.start()
    _cleanup.monitor = monitor

    # Run claude
    C.log(f"Invoking claude (timeout {rc.max_time_minutes}m)...")
    claude_log = log_dir / f"round-{round_num}.log"
    claude_exit = run_claude(
        prompt, system_context, rc.max_budget_usd, rc.max_turns, claude_log,
        timeout_minutes=rc.max_time_minutes,
    )
    C.log(f"Claude finished (exit {claude_exit})")

    monitor.stop()
    _cleanup.monitor = None

    def _finish(status: str) -> None:
        elapsed = int(time.time() - round_start)
        snapshot = get_resource_snapshot(gpu_type)
        C.log(
            f"Round {C.BOLD}{rc.name}{C.NC} {status} in "
            f"{format_duration(elapsed)} | {snapshot}"
        )

    if claude_exit != 0:
        C.err(f"Claude exited with code {claude_exit} in round {round_num} ({rc.name})")
        _finish("failed")
        if rc.gate == "soft":
            return RoundOutcome.SOFT_FAILED
        return RoundOutcome.HARD_FAILED

    # Check if any files changed
    has_changes = (
        git("diff", "--quiet", check=False).returncode != 0
        or git("diff", "--cached", "--quiet", check=False).returncode != 0
        or bool(git_untracked_files() - pre_untracked)
    )

    if not has_changes:
        C.warn(f"No changes made in round {round_num} ({rc.name}) — skipping commit")
        _finish("no changes")
        return RoundOutcome.NO_CHANGES

    # Stage changes
    git_stage_round_changes(pre_untracked)

    # Skip test verification for gate=none
    if rc.gate == "none":
        commit_msg = f"{rc.commit_message_prefix}{rc.name} (round {round_num}/{total_rounds})"
        git_commit(commit_msg)
        C.ok(f"Committed: {commit_msg} (gate=none, tests skipped)")
        run_reviewer(round_num, rc, pre_sha, log_dir, review_flag)
        _finish("passed")
        return RoundOutcome.PASSED

    # --- Test + retry loop ---
    test_output_file = _cleanup.make_temp()
    attempt = 0
    tests_passed = False

    while True:
        C.log(f"Running tests: {test_cmd}")
        test_exit = run_tests_with_tee(test_cmd, test_output_file)

        if test_exit == 0:
            C.ok("Tests passed")
            tests_passed = True
            break

        C.err("Tests failed")
        attempt += 1

        if attempt > rc.max_retries:
            break

        C.warn(f"Retry {attempt}/{rc.max_retries}: re-invoking Claude to fix test failures...")

        # Build retry prompt with last 100 lines
        test_lines = test_output_file.read_text().splitlines()
        test_tail = "\n".join(test_lines[-100:])
        retry_prompt = (
            "The tests are failing after your changes. Here is the test output:\n\n"
            f"```\n{test_tail}\n```\n\n"
            "Fix the test failures. Do not revert your previous work — fix the "
            "issues causing the failures. Run the tests after your fixes."
        )

        retry_budget = max(1.0, rc.max_budget_usd / 2)
        retry_log = log_dir / f"round-{round_num}-retry-{attempt}.log"
        retry_exit = run_claude(
            retry_prompt, system_context, retry_budget, rc.max_turns, retry_log,
            timeout_minutes=max(5, rc.max_time_minutes // 2),
        )
        C.log(f"Retry claude finished (exit {retry_exit})")

        # Re-stage changes
        git_stage_round_changes(pre_untracked)

    if not tests_passed:
        C.err(
            f"Tests failed after round {round_num} ({rc.name}) "
            f"(exhausted {rc.max_retries} retries)"
        )
        C.err("Rolling back changes from this round...")
        git_rollback_round(pre_untracked)
        _finish("tests failed")
        if rc.gate == "soft":
            return RoundOutcome.SOFT_FAILED
        return RoundOutcome.HARD_FAILED

    # Commit
    commit_msg = f"{rc.commit_message_prefix}{rc.name} (round {round_num}/{total_rounds})"
    git_commit(commit_msg)
    C.ok(f"Committed: {commit_msg}")

    # Run reviewer
    run_reviewer(round_num, rc, pre_sha, log_dir, review_flag)

    _finish("passed")
    return RoundOutcome.PASSED


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------


def pipeline(
    project_dir: str | None,
    rounds_arg: str | None,
    config_file: str | None,
    start_from: int,
    dry_run: bool,
    worktree: bool,
    worktree_symlinks: str | None,
    test_command: str | None,
    review_flag: bool | None,
    log_dir_arg: str | None,
) -> None:
    """Main pipeline orchestrator."""
    # Change to project directory
    if project_dir:
        pdir = Path(project_dir)
        if not pdir.is_dir():
            C.err(f"Project directory does not exist: {project_dir}")
            sys.exit(1)
        os.chdir(pdir)
        C.log(f"Working in: {project_dir}")

    # Ensure we're in a git repo
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        C.err("Not inside a git repository. Please run from a project directory.")
        sys.exit(1)

    # Acquire lock
    lock_path = git_acquire_lock(dry_run)
    if lock_path:
        _cleanup.lock_dir = lock_path

    # Check for uncommitted changes (non-worktree, non-dry-run)
    if not dry_run and not worktree:
        if git_has_uncommitted():
            C.err("Tracked files have uncommitted changes.")
            C.err("The pipeline would stage them along with its own changes.")
            C.err("Options:")
            C.err("  1. Commit or stash your changes first")
            C.err("  2. Use --worktree to run in an isolated worktree (safe for Claude Code)")
            sys.exit(1)

    # Load config
    config = PipelineConfig()
    if config_file:
        config = load_pipeline_config(Path(config_file))
    elif Path(".claude/pipeline.yaml").exists():
        C.log("Found .claude/pipeline.yaml — loading config")
        config = load_pipeline_config(Path(".claude/pipeline.yaml"))

    # Apply config (CLI takes precedence)
    effective_test_cmd = test_command or config.test_command or ""
    branch_prefix = config.branch_prefix or BRANCH_PREFIX_DEFAULT

    # Parse requested rounds
    requested_rounds: list[str] = []
    if rounds_arg:
        requested_rounds = rounds_arg.split()
    elif config.rounds:
        requested_rounds = config.rounds

    # Detect test command
    if not effective_test_cmd:
        C.log("Auto-detecting test command...")
        detected = detect_test_command(Path.cwd())
        if detected:
            effective_test_cmd = detected
            C.ok(f"Detected test command: {effective_test_cmd}")
        else:
            C.err("Could not auto-detect test command.")
            C.err("Specify with --test-command or add to .claude/pipeline.yaml")
            sys.exit(1)
    else:
        C.log(f"Using test command: {effective_test_cmd}")

    # Detect GPU
    gpu_type = detect_gpu()
    if gpu_type != "none":
        C.log(f"GPU monitoring: {gpu_type}")

    # Resolve round files
    round_files: list[Path] = []
    if requested_rounds:
        for name in requested_rounds:
            f = resolve_round_file(name)
            if f:
                round_files.append(f)
            else:
                C.err(f"Unknown round: {name}")
                C.err("Available rounds:")
                for rf in discover_rounds():
                    rc = parse_frontmatter(rf)
                    C.err(f"  - {rc.name}")
                sys.exit(1)
    else:
        round_files = discover_rounds()

    total = len(round_files)
    if total == 0:
        C.err("No rounds found.")
        sys.exit(1)

    # Validate start_from
    if start_from > total:
        C.err(f"--start-from {start_from} exceeds total rounds ({total})")
        sys.exit(1)

    # Create branch (and worktree if requested)
    short_sha = git("rev-parse", "--short", "HEAD").stdout.strip()
    branch_name = f"{branch_prefix}/{time.strftime('%Y-%m-%d')}-{short_sha}"

    symlink_dirs = (
        worktree_symlinks.split() if worktree_symlinks else DEFAULT_SYMLINK_DIRS
    )

    if dry_run:
        C.log(f"[DRY RUN] Would create branch: {branch_name}")
        if worktree:
            C.log("[DRY RUN] Would create isolated worktree for branch")
    elif worktree:
        _cleanup.worktree_mode = True
        # Warn about stale worktrees
        try:
            wt_list = git("worktree", "list", "--porcelain").stdout
            stale = [
                line.replace("worktree ", "")
                for line in wt_list.splitlines()
                if line.startswith("worktree ") and "quality-worktree" in line
            ]
            if stale:
                C.warn("Found existing quality worktree(s):")
                for wt in stale:
                    C.warn(f"  {wt}")
                C.warn("If stale, clean up with: git worktree remove <path>")
        except Exception:
            pass

        wt_dir, orig_dir = setup_worktree(branch_name, symlink_dirs)
        _cleanup.worktree_dir = wt_dir
        _cleanup.original_dir = orig_dir
        _cleanup.symlink_dirs = symlink_dirs
    else:
        git_create_branch(branch_name)

    # Create log directory
    if log_dir_arg:
        log_dir = Path(log_dir_arg)
        log_dir.mkdir(parents=True, exist_ok=True)
    else:
        log_dir = Path(tempfile.mkdtemp(prefix=f"quality-pipeline-{os.getpid()}-"))
    C.log(f"Log directory: {log_dir}")

    # Print plan
    print()
    C.log(f"{C.BOLD}Quality Pipeline Plan{C.NC}")
    C.log(f"Branch: {branch_name}")
    C.log(f"Test command: {effective_test_cmd}")
    C.log(f"Rounds: {total} (starting from {start_from})")
    for i, rf in enumerate(round_files):
        n = i + 1
        rc = parse_frontmatter(rf)
        rc = apply_config_overrides(rc, config)
        marker = ""
        if n < start_from:
            marker = " (skip)"
        elif n == start_from:
            marker = " \u2190 start"
        review_str = str(rc.review).lower() if rc.review is not None else "false"
        analyzers_str = rc.analyzers or "none"
        C.log(
            f"  {n}. {rc.name} [${rc.max_budget_usd:.2f}, {rc.max_time_minutes}m] "
            f"gate={rc.gate} retries={rc.max_retries} review={review_str} "
            f"analyzers={analyzers_str}{marker}"
        )
    print()

    if dry_run:
        # Show dry-run details per round
        for i, rf in enumerate(round_files):
            n = i + 1
            if n < start_from:
                continue
            rc = parse_frontmatter(rf)
            rc = apply_config_overrides(rc, config)
            prompt = get_round_prompt(rf)
            prompt = apply_config_prompt_append(rc.name, prompt, config)

            _print_round_header(n, total, rc)

            # Review status
            review_status: str
            if review_flag is not None:
                review_status = f"{str(review_flag).lower()} (CLI)"
            else:
                ov = _find_override(rc.name, config)
                if "review" in ov:
                    review_status = f"{str(ov['review']).lower()} (config)"
                else:
                    review_status = (
                        f"{str(rc.review).lower() if rc.review is not None else 'false'} "
                        f"(frontmatter)"
                    )
            analyzers_status = rc.analyzers or "none"
            C.log(f"[DRY RUN] Would run claude -p with {len(prompt)} chars of prompt")
            C.log(f"[DRY RUN] Would run tests: {effective_test_cmd}")
            C.log(f"[DRY RUN] Would commit with prefix: {rc.commit_message_prefix}")
            C.log(f"[DRY RUN] Gate: {rc.gate} | Max retries: {rc.max_retries}")
            C.log(f"[DRY RUN] Review: {review_status} | Analyzers: {analyzers_status}")

        C.ok("Dry run complete. No changes made.")
        return

    # --- Run rounds ---
    pipeline_start = time.time()
    results: list[RoundResult] = []

    for i, rf in enumerate(round_files):
        n = i + 1
        rc = parse_frontmatter(rf)

        if n < start_from:
            results.append(RoundResult(rc.name, RoundOutcome.SKIPPED))
            continue

        outcome = run_round(
            rf, n, total, effective_test_cmd, config, review_flag, log_dir, gpu_type
        )

        results.append(RoundResult(rc.name, outcome))

        if outcome == RoundOutcome.HARD_FAILED:
            C.err(f"Pipeline stopped at round {n} (hard gate failure).")
            if n < total:
                C.warn(f"Resume with: quality-pipeline --start-from {n + 1}")
            break

        if outcome == RoundOutcome.SOFT_FAILED:
            C.warn(f"Round {n} failed (soft gate) — continuing to next round.")

    # --- Summary ---
    pipeline_elapsed = int(time.time() - pipeline_start)
    print()
    C.log("\u2501" * 60)
    C.log(f"{C.BOLD}Pipeline Summary{C.NC}")
    C.log(f"Branch: {branch_name}")
    C.log(f"Total time: {format_duration(pipeline_elapsed)}")
    C.log(f"Resources: {get_resource_snapshot(gpu_type)}")
    print()

    C.log(f"{C.BOLD}Per-round results:{C.NC}")
    outcome_colors = {
        RoundOutcome.PASSED: f"{C.GREEN}passed{C.NC}",
        RoundOutcome.NO_CHANGES: f"{C.BLUE}no-changes{C.NC}",
        RoundOutcome.SOFT_FAILED: f"{C.YELLOW}soft-failed{C.NC}",
        RoundOutcome.HARD_FAILED: f"{C.RED}HARD-FAILED{C.NC}",
        RoundOutcome.SKIPPED: "skipped",
    }
    passed = sum(1 for r in results if r.outcome in (RoundOutcome.PASSED, RoundOutcome.NO_CHANGES))
    hard_failed = sum(1 for r in results if r.outcome == RoundOutcome.HARD_FAILED)
    soft_failed = sum(1 for r in results if r.outcome == RoundOutcome.SOFT_FAILED)
    skipped = sum(1 for r in results if r.outcome == RoundOutcome.SKIPPED)

    for i, r in enumerate(results):
        C.log(f"  {i + 1}. {r.name}: {outcome_colors.get(r.outcome, str(r.outcome.value))}")

    print()
    C.ok(f"Passed: {passed}")
    if skipped > 0:
        C.warn(f"Skipped: {skipped}")
    if soft_failed > 0:
        C.warn(f"Soft failures: {soft_failed} (continued past)")
    if hard_failed > 0:
        C.err(f"Hard failures: {hard_failed} (stopped pipeline)")

    print()
    C.log(f"{C.BOLD}Log directory: {log_dir}{C.NC}")
    for lf in sorted(log_dir.glob("*")):
        if lf.suffix in (".log", ".json"):
            C.log(f"  {lf}")
    C.log("\u2501" * 60)

    if hard_failed > 0:
        sys.exit(1)

    C.ok(f"Pipeline complete. Review commits with: git log --oneline {branch_name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command()
@click.option("--project-dir", default=None, help="Run in DIR instead of current directory")
@click.option("--rounds", default=None, help='Rounds to run (space-separated, e.g. "r1 r2")')
@click.option("--config", "config_file", default=None, help="Path to pipeline.yaml config")
@click.option(
    "--start-from", default=1, type=int, show_default=True,
    help="Start from round N (1-indexed, for resuming)",
)
@click.option("--dry-run", is_flag=True, help="Show plan without executing")
@click.option("--worktree", is_flag=True, help="Run in an isolated git worktree")
@click.option(
    "--worktree-symlinks", default=None,
    help="Space-separated dirs to symlink into worktree",
)
@click.option("--test-command", default=None, help="Override auto-detected test command")
@click.option("--review/--no-review", default=None, help="Force reviewer pass on/off")
@click.option("--log-dir", default=None, help="Directory for log files")
def cli(
    project_dir: str | None,
    rounds: str | None,
    config_file: str | None,
    start_from: int,
    dry_run: bool,
    worktree: bool,
    worktree_symlinks: str | None,
    test_command: str | None,
    review: bool | None,
    log_dir: str | None,
) -> None:
    """Multi-round automated code quality pipeline.

    Orchestrates sequential `claude -p` invocations, each with a focused
    objective, test verification, and a clean git commit.
    """
    if start_from < 1:
        raise click.BadParameter(
            f"must be a positive integer (got: {start_from})", param_hint="--start-from"
        )

    pipeline(
        project_dir=project_dir,
        rounds_arg=rounds,
        config_file=config_file,
        start_from=start_from,
        dry_run=dry_run,
        worktree=worktree,
        worktree_symlinks=worktree_symlinks,
        test_command=test_command,
        review_flag=review,
        log_dir_arg=log_dir,
    )


if __name__ == "__main__":
    cli()
