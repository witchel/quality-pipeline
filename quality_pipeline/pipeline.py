"""Pipeline orchestrator: round execution and main pipeline loop."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .output import C, atomic_write_text, format_duration, gate_label
from .config import (
    BRANCH_PREFIX_DEFAULT,
    DEFAULT_SYMLINK_DIRS,
    PipelineConfig,
    RoundConfig,
    RoundOutcome,
    RoundResult,
    _find_override,
    apply_config_overrides,
    apply_config_prompt_append,
    discover_rounds,
    get_round_prompt,
    load_pipeline_config,
    parse_frontmatter,
    resolve_round_file,
)
from .monitoring import ResourceMonitor, detect_gpu, get_resource_snapshot
from .detection import detect_test_command, run_static_analysis
from .git_ops import (
    git,
    git_acquire_lock,
    git_commit,
    git_create_branch,
    git_has_uncommitted,
    git_rev_parse_head,
    git_rollback_round,
    git_stage_round_changes,
    git_untracked_files,
    setup_worktree,
)
from .process import run_claude, run_reviewer, run_tests_with_tee
from .cleanup import _cleanup

_MIN_TEST_TIMEOUT_SECS = 60
_TEST_FAILURE_TAIL_LINES = 100
_MIN_RETRY_BUDGET_USD = 1.0


def _remaining_seconds(round_start: float, max_minutes: int) -> int:
    """Seconds remaining in the round's time budget."""
    return int(max_minutes * 60 - (time.time() - round_start))


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


def _check_review_verdict(verdict: str | None, rc: RoundConfig) -> RoundOutcome:
    """Map a reviewer verdict + review_gate config to a round outcome."""
    if verdict is None or verdict != "critical" or rc.review_gate == "none":
        return RoundOutcome.PASSED
    C.err(
        f"Reviewer found CRITICAL issues (review_gate={rc.review_gate}) "
        f"— treating as {rc.review_gate} gate failure"
    )
    if rc.review_gate == "hard":
        return RoundOutcome.HARD_FAILED
    return RoundOutcome.SOFT_FAILED


def run_round(
    round_file: Path,
    round_num: int,
    total_rounds: int,
    test_cmd: str,
    config: PipelineConfig,
    review_flag: bool | None,
    log_dir: Path,
    gpu_type: str,
    rc: RoundConfig | None = None,
) -> RoundOutcome:
    """Execute a single pipeline round. Returns the outcome."""
    round_start = time.time()
    pre_sha = git_rev_parse_head()

    if rc is None:
        rc = apply_config_overrides(parse_frontmatter(round_file), config)
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
    # _finalize_round_config guarantees these are set
    assert rc.max_budget_usd is not None
    assert rc.max_turns is not None
    assert rc.max_time_minutes is not None

    prompt = get_round_prompt(round_file)
    prompt = apply_config_prompt_append(rc.name, prompt, config)

    _print_round_header(round_num, total_rounds, rc)

    # Build system context
    system_context = (
        f"You are running as part of an automated quality pipeline.\n"
        f"This is round {round_num} of {total_rounds}: {rc.name}.\n"
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
        # Save to log dir for post-hoc debugging
        atomic_write_text(log_dir / f"analysis-round-{round_num}.txt", analysis_output)
        prompt += (
            "\n\n## Static Analysis Results\n"
            "The following issues were found by static analysis tools. "
            "Use these as a starting point:\n" + analysis_output
        )

    # Snapshot untracked files before this round
    pre_untracked = git_untracked_files()

    # Log initial resource state and start monitor
    C.log(f"Resources: {get_resource_snapshot(gpu_type)}")
    monitor = ResourceMonitor(60, gpu_type, round_start)
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
        verdict = run_reviewer(round_num, rc, pre_sha, log_dir, review_flag)
        outcome = _check_review_verdict(verdict, rc)
        _finish("passed" if outcome == RoundOutcome.PASSED else "review-failed")
        return outcome

    # --- Test + retry loop ---
    test_output_file = _cleanup.make_temp()
    attempt = 0
    tests_passed = False

    while True:
        test_remaining = max(_MIN_TEST_TIMEOUT_SECS, _remaining_seconds(round_start, rc.max_time_minutes))
        C.log(f"Running tests: {test_cmd}")
        test_exit = run_tests_with_tee(
            test_cmd, test_output_file, timeout_seconds=test_remaining,
        )

        if test_exit == 0:
            C.ok("Tests passed")
            tests_passed = True
            break

        C.err("Tests failed")
        attempt += 1

        if attempt > rc.max_retries:
            break

        remaining = _remaining_seconds(round_start, rc.max_time_minutes)
        if remaining <= _MIN_TEST_TIMEOUT_SECS:
            C.warn("Round time budget exhausted — stopping retries")
            break

        C.warn(f"Retry {attempt}/{rc.max_retries}: re-invoking Claude to fix test failures...")

        # Build retry prompt with last 100 lines
        test_lines = test_output_file.read_text().splitlines()
        test_tail = "\n".join(test_lines[-_TEST_FAILURE_TAIL_LINES:])
        retry_prompt = (
            "The tests are failing after your changes. Here is the test output:\n\n"
            f"```\n{test_tail}\n```\n\n"
            "Fix the test failures. Do not revert your previous work — fix the "
            "issues causing the failures. Run the tests after your fixes."
        )

        retry_budget = max(_MIN_RETRY_BUDGET_USD, rc.max_budget_usd / 2)
        retry_log = log_dir / f"round-{round_num}-retry-{attempt}.log"
        retry_exit = run_claude(
            retry_prompt, system_context, retry_budget, rc.max_turns, retry_log,
            timeout_minutes=max(1, remaining // 60),
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

    # Run reviewer — verdict may downgrade the round outcome
    verdict = run_reviewer(round_num, rc, pre_sha, log_dir, review_flag)
    outcome = _check_review_verdict(verdict, rc)
    _finish("passed" if outcome == RoundOutcome.PASSED else "review-failed")
    return outcome


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
    _cleanup.activate()

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
        except (subprocess.CalledProcessError, OSError):
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

    # Parse all rounds once upfront
    round_configs = [
        (rf, apply_config_overrides(parse_frontmatter(rf), config))
        for rf in round_files
    ]

    # Print plan
    print()
    C.log(f"{C.BOLD}Quality Pipeline Plan{C.NC}")
    C.log(f"Branch: {branch_name}")
    C.log(f"Test command: {effective_test_cmd}")
    C.log(f"Rounds: {total} (starting from {start_from})")
    for i, (rf, rc) in enumerate(round_configs):
        n = i + 1
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
        for i, (rf, rc) in enumerate(round_configs):
            n = i + 1
            if n < start_from:
                continue
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

    for i, (rf, rc) in enumerate(round_configs):
        n = i + 1

        if n < start_from:
            results.append(RoundResult(rc.name, RoundOutcome.SKIPPED))
            continue

        outcome = run_round(
            rf, n, total, effective_test_cmd, config, review_flag, log_dir, gpu_type,
            rc=rc,
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
        if lf.suffix in (".log", ".json", ".txt"):
            C.log(f"  {lf}")
    C.log("\u2501" * 60)

    if hard_failed > 0:
        sys.exit(1)

    C.ok(f"Pipeline complete. Review commits with: git log --oneline {branch_name}")
