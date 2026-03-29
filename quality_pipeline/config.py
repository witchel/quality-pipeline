"""Constants, dataclasses, frontmatter parsing, config resolution, and round discovery."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from glob import escape as _glob_escape
from pathlib import Path
from typing import Any

import yaml

from .output import C

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PACKAGE_DIR = Path(__file__).resolve().parent
ROUNDS_DIR = _PACKAGE_DIR / "rounds"
TEMPLATE_DIR = _PACKAGE_DIR / "templates"

DEFAULT_SYMLINK_DIRS: list[str] = [
    "node_modules", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    ".next", ".nuxt", "vendor", ".bundle",
]

ENV_FILES = [".env", ".env.local", ".env.test"]

BRANCH_PREFIX_DEFAULT = "quality"

DEFAULT_ANALYZERS: dict[str, str] = {
    "security": "bandit semgrep ruff-security",
    "type-safety": "mypy pyright tsc",
    "dead-code": "vulture ruff-dead-code codegraph-unused",
    "simplify": "ruff-simplify",
    "refactor": "ruff-refactor",
}

MAX_ANALYSIS_OUTPUT = 4000

VALID_GATES = {"hard", "soft", "none"}

_DEFAULT_MAX_BUDGET_USD = 5.00
_DEFAULT_MAX_TURNS = 30
_DEFAULT_MAX_TIME_MINUTES = 20

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
    review_gate: str = "none"  # none=advisory, soft/hard=verdict can fail round
    analyzers: str = ""
    bookend: bool = False


@dataclass
class PipelineConfig:
    test_command: str = ""
    rounds: list[str] = field(default_factory=list)
    branch_prefix: str = ""
    max_budget_usd: float | None = None
    max_turns: int | None = None
    max_time_minutes: int | None = None
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


class RoundOutcome(Enum):
    PASSED = "passed"
    NO_CHANGES = "no-changes"
    HARD_FAILED = "HARD-FAILED"
    SOFT_FAILED = "soft-failed"
    SKIPPED = "skipped"


@dataclass
class DiffStats:
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    stat_summary: str = ""   # e.g. "3 files, +42/-18"
    stat_detail: str = ""    # full git diff --stat output


@dataclass
class RoundResult:
    name: str
    outcome: RoundOutcome
    elapsed_seconds: int = 0
    diff_stats: DiffStats | None = None
    commit_sha: str = ""
    reviewer_verdict: str = ""
    change_summary: str = ""


# ---------------------------------------------------------------------------
# Frontmatter & config
# ---------------------------------------------------------------------------


def _parse_review_bool(val: bool | str | None) -> bool | None:
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
    except yaml.YAMLError as e:
        C.warn(f"Failed to parse frontmatter in {path.name}: {e}")
        return RoundConfig()

    review = _parse_review_bool(data.get("review"))
    bookend = _parse_review_bool(data.get("bookend"))

    try:
        return RoundConfig(
            name=str(data.get("name", "")),
            commit_message_prefix=str(data.get("commit_message_prefix", "chore: ")),
            max_budget_usd=float(data["max_budget_usd"]) if "max_budget_usd" in data else None,
            max_turns=int(data["max_turns"]) if "max_turns" in data else None,
            max_time_minutes=int(data["max_time_minutes"]) if "max_time_minutes" in data else None,
            gate=str(data.get("gate", "hard")),
            max_retries=int(data.get("max_retries", 0)),
            review=review,
            review_gate=str(data.get("review_gate", "none")),
            analyzers=str(data.get("analyzers", "")),
            bookend=bool(bookend) if bookend is not None else False,
        )
    except (ValueError, TypeError) as e:
        C.warn(f"Invalid value in frontmatter of {path.name}: {e}")
        return RoundConfig()


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
    raw_turns = data.get("max_turns")
    try:
        return PipelineConfig(
            test_command=str(data.get("test_command", "")),
            rounds=list(data.get("rounds", [])),
            branch_prefix=str(data.get("branch_prefix", "")),
            max_budget_usd=float(data["max_budget_usd"]) if "max_budget_usd" in data else None,
            max_turns=int(raw_turns) if raw_turns is not None else None,
            max_time_minutes=int(raw_time) if raw_time is not None else None,
            overrides=overrides,
        )
    except (ValueError, TypeError) as e:
        C.warn(f"Invalid value in pipeline config: {e}")
        return PipelineConfig()


def _find_override(name: str, config: PipelineConfig) -> dict[str, Any]:
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
    """Fill in defaults for unset fields and validate gate/numeric values."""
    changes: dict[str, float | int | bool | str] = {}
    if rc.max_budget_usd is None:
        changes["max_budget_usd"] = _DEFAULT_MAX_BUDGET_USD
    if rc.max_turns is None:
        changes["max_turns"] = _DEFAULT_MAX_TURNS
    if rc.max_time_minutes is None:
        changes["max_time_minutes"] = _DEFAULT_MAX_TIME_MINUTES
    if rc.review is None:
        changes["review"] = False
    if rc.gate not in VALID_GATES:
        C.warn(f"Unknown gate '{rc.gate}' for round '{rc.name}' — defaulting to 'hard'")
        changes["gate"] = "hard"
    if rc.review_gate not in VALID_GATES:
        C.warn(
            f"Unknown review_gate '{rc.review_gate}' for round '{rc.name}' "
            f"— defaulting to 'none'"
        )
        changes["review_gate"] = "none"
    rc = replace(rc, **changes) if changes else rc
    # Validate positive numeric values
    budget = rc.max_budget_usd
    turns = rc.max_turns
    time_m = rc.max_time_minutes
    if budget is not None and budget <= 0:
        C.warn(f"max_budget_usd={budget} for '{rc.name}' is not positive — using default")
        rc = replace(rc, max_budget_usd=_DEFAULT_MAX_BUDGET_USD)
    if turns is not None and turns <= 0:
        C.warn(f"max_turns={turns} for '{rc.name}' is not positive — using default")
        rc = replace(rc, max_turns=_DEFAULT_MAX_TURNS)
    if time_m is not None and time_m <= 0:
        C.warn(f"max_time_minutes={time_m} for '{rc.name}' is not positive — using default")
        rc = replace(rc, max_time_minutes=_DEFAULT_MAX_TIME_MINUTES)
    return rc


def apply_config_overrides(rc: RoundConfig, config: PipelineConfig) -> RoundConfig:
    """Apply per-round overrides then global defaults, returning a finalized RoundConfig.

    Priority (highest first): per-round override > frontmatter > global config > default.
    """
    ov = _find_override(rc.name, config)
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

    if "max_turns" in ov:
        rc.max_turns = int(ov["max_turns"])
    elif rc.max_turns is None and config.max_turns is not None:
        rc.max_turns = config.max_turns

    if "gate" in ov:
        rc.gate = str(ov["gate"])
    if "max_retries" in ov:
        rc.max_retries = int(ov["max_retries"])
    if "review" in ov:
        rc.review = _parse_review_bool(ov["review"])
    if "review_gate" in ov:
        rc.review_gate = str(ov["review_gate"])
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

    # Try filename pattern matching (escape glob chars in user input)
    escaped = _glob_escape(name)
    for pattern in [f"*-{escaped}.md", f"*{escaped}*.md"]:
        matches = list(ROUNDS_DIR.glob(pattern))
        if matches:
            return matches[0]
    return None
