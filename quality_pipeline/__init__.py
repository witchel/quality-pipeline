"""quality_pipeline — Multi-round automated code quality pipeline.

Re-exports all public and tested names for backward compatibility.
"""

from __future__ import annotations

# Expose submodules as attributes
from . import cleanup as cleanup  # noqa: F401
from . import config as config  # noqa: F401
from . import detection as detection  # noqa: F401
from . import git_ops as git_ops  # noqa: F401
from . import monitoring as monitoring  # noqa: F401
from . import output as output  # noqa: F401
from . import pipeline as pipeline_mod  # noqa: F401
from . import process as process  # noqa: F401

# --- output.py ---
from .output import ColorOutput as ColorOutput, C as C, format_duration as format_duration, gate_label as gate_label

# --- config.py ---
from .config import (
    ROUNDS_DIR as ROUNDS_DIR,
    TEMPLATE_DIR as TEMPLATE_DIR,
    DEFAULT_SYMLINK_DIRS as DEFAULT_SYMLINK_DIRS,
    ENV_FILES as ENV_FILES,
    BRANCH_PREFIX_DEFAULT as BRANCH_PREFIX_DEFAULT,
    DEFAULT_ANALYZERS as DEFAULT_ANALYZERS,
    MAX_ANALYSIS_OUTPUT as MAX_ANALYSIS_OUTPUT,
    VALID_GATES as VALID_GATES,
    _DEFAULT_MAX_BUDGET_USD as _DEFAULT_MAX_BUDGET_USD,
    _DEFAULT_MAX_TURNS as _DEFAULT_MAX_TURNS,
    _DEFAULT_MAX_TIME_MINUTES as _DEFAULT_MAX_TIME_MINUTES,
    RoundConfig as RoundConfig,
    PipelineConfig as PipelineConfig,
    RoundOutcome as RoundOutcome,
    RoundResult as RoundResult,
    _parse_review_bool as _parse_review_bool,
    parse_frontmatter as parse_frontmatter,
    get_round_prompt as get_round_prompt,
    load_pipeline_config as load_pipeline_config,
    _find_override as _find_override,
    _finalize_round_config as _finalize_round_config,
    apply_config_overrides as apply_config_overrides,
    apply_config_prompt_append as apply_config_prompt_append,
    discover_rounds as discover_rounds,
    resolve_round_file as resolve_round_file,
)

# --- monitoring.py ---
from .monitoring import detect_gpu as detect_gpu, get_resource_snapshot as get_resource_snapshot, ResourceMonitor as ResourceMonitor

# --- detection.py ---
from .detection import detect_test_command as detect_test_command, _run_analyzer as _run_analyzer, run_static_analysis as run_static_analysis

# --- git_ops.py ---
from .git_ops import (
    git as git,
    git_rev_parse_head as git_rev_parse_head,
    git_has_uncommitted as git_has_uncommitted,
    git_untracked_files as git_untracked_files,
    git_stage_round_changes as git_stage_round_changes,
    git_rollback_round as git_rollback_round,
    git_create_branch as git_create_branch,
    git_commit as git_commit,
    git_acquire_lock as git_acquire_lock,
    setup_worktree as setup_worktree,
)

# --- cleanup.py ---
from .cleanup import PipelineCleanup as PipelineCleanup, _cleanup as _cleanup, _handle_signal as _handle_signal

# --- process.py ---
from .process import (
    _kill_process_group as _kill_process_group,
    run_tests_with_tee as run_tests_with_tee,
    _claude_env as _claude_env,
    _run_claude_process as _run_claude_process,
    run_claude as run_claude,
    _parse_verdict as _parse_verdict,
    run_reviewer as run_reviewer,
)

# --- pipeline.py ---
from .pipeline import (
    _print_round_header as _print_round_header,
    _check_review_verdict as _check_review_verdict,
    run_round as run_round,
    pipeline as pipeline,
)

# --- __main__.py ---
from .__main__ import cli as cli
