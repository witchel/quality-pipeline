"""quality_pipeline — Multi-round automated code quality pipeline.

Re-exports all public and tested names for backward compatibility.
"""

from __future__ import annotations

# Expose submodules as attributes
from . import cleanup  # noqa: F401
from . import config  # noqa: F401
from . import detection  # noqa: F401
from . import git_ops  # noqa: F401
from . import monitoring  # noqa: F401
from . import output  # noqa: F401
from . import pipeline as pipeline_mod  # noqa: F401
from . import process  # noqa: F401

# --- output.py ---
from .output import ColorOutput, C, format_duration, gate_label

# --- config.py ---
from .config import (
    PLUGIN_DIR,
    ROUNDS_DIR,
    TEMPLATE_DIR,
    DEFAULT_SYMLINK_DIRS,
    ENV_FILES,
    BRANCH_PREFIX_DEFAULT,
    DEFAULT_ANALYZERS,
    MAX_ANALYSIS_OUTPUT,
    VALID_GATES,
    _DEFAULT_MAX_BUDGET_USD,
    _DEFAULT_MAX_TURNS,
    _DEFAULT_MAX_TIME_MINUTES,
    RoundConfig,
    PipelineConfig,
    RoundOutcome,
    RoundResult,
    _parse_review_bool,
    parse_frontmatter,
    get_round_prompt,
    load_pipeline_config,
    _find_override,
    _finalize_round_config,
    apply_config_overrides,
    apply_config_prompt_append,
    discover_rounds,
    resolve_round_file,
)

# --- monitoring.py ---
from .monitoring import detect_gpu, get_resource_snapshot, ResourceMonitor

# --- detection.py ---
from .detection import detect_test_command, _run_analyzer, run_static_analysis

# --- git_ops.py ---
from .git_ops import (
    git,
    git_rev_parse_head,
    git_has_uncommitted,
    git_untracked_files,
    git_stage_round_changes,
    git_rollback_round,
    git_create_branch,
    git_commit,
    git_acquire_lock,
    setup_worktree,
)

# --- cleanup.py ---
from .cleanup import PipelineCleanup, _cleanup, _handle_signal

# --- process.py ---
from .process import (
    _kill_process_group,
    run_tests_with_tee,
    _claude_env,
    run_claude,
    _parse_verdict,
    run_reviewer,
)

# --- pipeline.py ---
from .pipeline import (
    _print_round_header,
    _check_review_verdict,
    run_round,
    pipeline,
)

# --- __main__.py ---
from .__main__ import cli
