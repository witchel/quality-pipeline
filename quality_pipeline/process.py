"""Subprocess management: test runner, claude invocation, reviewer."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .output import C
from .config import TEMPLATE_DIR, RoundConfig
from .git_ops import git

REVIEWER_BUDGET_USD = 1.00
REVIEWER_MAX_TURNS = 5
REVIEWER_TIMEOUT_MINUTES = 10
REVIEWER_MAX_DIFF_CHARS = 8000


def _kill_process_group(proc: subprocess.Popen, graceful_wait: float = 2.0) -> None:
    """Kill a process and its entire process group.

    Requires the process to have been started with ``start_new_session=True``
    so it has its own process group.  Sends SIGTERM first, then SIGKILL if
    the process doesn't exit within *graceful_wait* seconds.
    """
    if proc.pid is None or proc.pid <= 0:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        return
    try:
        proc.wait(timeout=graceful_wait)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def run_tests_with_tee(
    cmd: str, output_file: Path, timeout_seconds: int = 0,
) -> int:
    """Run tests, teeing output to both stdout and a file. Returns exit code.

    If *timeout_seconds* > 0, the test process is killed after that many
    seconds and exit code ``-1`` is returned.

    The child is started in its own session/process-group so that the
    timeout can kill the entire tree, and ``stdbuf -oL`` (or macOS
    ``gstdbuf``) is prepended when available to force line-buffered output
    from the child — otherwise libc block-buffers when stdout is a pipe.
    """
    timed_out = False

    # Force line-buffered stdout from the child process.  Without this,
    # libc detects a pipe and block-buffers (typically 4-8 KB), so test
    # output appears in delayed bursts rather than line-by-line.
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"  # covers pytest and most Python runners
    for stdbuf in ("stdbuf", "gstdbuf"):
        if shutil.which(stdbuf):
            cmd = f"{stdbuf} -oL {cmd}"
            break

    def _kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        _kill_process_group(proc)

    proc = subprocess.Popen(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, start_new_session=True, env=env,
    )
    timer: threading.Timer | None = None
    if timeout_seconds > 0:
        timer = threading.Timer(timeout_seconds, _kill_on_timeout)
        timer.start()
    try:
        with output_file.open("w") as fout:
            if proc.stdout is not None:
                for line in proc.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    fout.write(line)
        proc.wait()
    except BaseException:
        _kill_process_group(proc)
        proc.wait()
        raise
    finally:
        if timer is not None:
            timer.cancel()

    if timed_out:
        C.err(f"Tests timed out after {timeout_seconds}s")
        return -1
    return proc.returncode


def _claude_env() -> dict[str, str]:
    """Build an environment for spawning claude -p without recursive-run detection."""
    env = os.environ.copy()
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(var, None)
    return env


def _run_claude_process(
    cmd: list[str],
    output_file: Path,
    timeout_minutes: int = 0,
) -> tuple[int, bool]:
    """Spawn a claude subprocess, tee stderr to terminal, capture stdout to file.

    Stderr (progress messages) is tee'd to the terminal so the user can see
    activity.  Stdout (JSON result) is captured only to *output_file*.

    Returns ``(exit_code, timed_out)``.  If *timeout_minutes* > 0, the
    subprocess is killed after that many minutes.
    """
    env = _claude_env()
    timeout_secs = timeout_minutes * 60 if timeout_minutes > 0 else None
    timed_out = False

    def _kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        _kill_process_group(proc)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env, start_new_session=True,
    )
    timer: threading.Timer | None = None
    if timeout_secs is not None:
        timer = threading.Timer(timeout_secs, _kill_on_timeout)
        timer.start()
    stderr_thread: threading.Thread | None = None
    try:
        with output_file.open("w") as fout:
            def _tee_stderr() -> None:
                assert proc.stderr is not None
                for line in proc.stderr:
                    sys.stderr.write(line)
                    sys.stderr.flush()

            stderr_thread = threading.Thread(target=_tee_stderr, daemon=True)
            stderr_thread.start()

            if proc.stdout is not None:
                for line in proc.stdout:
                    fout.write(line)

            proc.wait()
    except BaseException:
        _kill_process_group(proc)
        proc.wait()
        raise
    finally:
        if timer is not None:
            timer.cancel()
        if stderr_thread is not None:
            stderr_thread.join(timeout=5)

    return proc.returncode, timed_out


def run_claude(
    prompt: str,
    system_ctx: str,
    budget: float,
    turns: int,
    log_file: Path,
    timeout_minutes: int = 0,
) -> int:
    """Invoke claude -p and capture output to log. Returns exit code."""
    cmd = [
        "claude", "-p", prompt,
        "--append-system-prompt", system_ctx,
        "--dangerously-skip-permissions",
        "--max-budget-usd", f"{budget:.2f}",
        "--max-turns", str(turns),
        "--output-format", "json",
    ]
    exit_code, timed_out = _run_claude_process(cmd, log_file, timeout_minutes)
    if timed_out:
        C.err(
            f"Claude timed out after {timeout_minutes} minutes — "
            f"increase max_time_minutes if the round needs more time"
        )
        return -1
    return exit_code


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
) -> str | None:
    """Run the reviewer pass if enabled. Returns verdict or None if skipped."""
    # CLI flag > config/frontmatter (already merged by apply_config_overrides)
    review_enabled = review_flag if review_flag is not None else rc.review
    if not review_enabled:
        return None

    C.log("Running reviewer pass...")

    # Get diff (check=False means git() won't raise on non-zero exit)
    diff_result = git("diff", pre_sha, "HEAD", check=False)
    diff_raw = diff_result.stdout or ""
    if len(diff_raw) > REVIEWER_MAX_DIFF_CHARS:
        diff_content = diff_raw[:REVIEWER_MAX_DIFF_CHARS] + (
            f"\n\n[... truncated {len(diff_raw) - REVIEWER_MAX_DIFF_CHARS} chars — "
            f"review may miss issues in the remainder ...]"
        )
    else:
        diff_content = diff_raw

    if not diff_content:
        C.warn("No diff to review — skipping reviewer")
        return None

    template_file = TEMPLATE_DIR / "reviewer.md"
    if not template_file.exists():
        C.warn(f"Reviewer template not found: {template_file} — skipping")
        return None

    review_prompt = template_file.read_text().replace("DIFF_PLACEHOLDER", diff_content)
    review_output = log_dir / f"review-round-{round_num}.json"

    cmd = [
        "claude", "-p", review_prompt,
        "--dangerously-skip-permissions",
        "--max-budget-usd", f"{REVIEWER_BUDGET_USD:.2f}",
        "--max-turns", str(REVIEWER_MAX_TURNS),
        "--output-format", "json",
    ]
    exit_code, timed_out = _run_claude_process(
        cmd, review_output, REVIEWER_TIMEOUT_MINUTES,
    )
    if timed_out:
        C.warn(f"Reviewer timed out after {REVIEWER_TIMEOUT_MINUTES}m — skipping review")
        return None
    C.log(f"Reviewer claude finished (exit {exit_code})")

    verdict = _parse_verdict(review_output.read_text())

    if verdict == "pass":
        C.ok(f"Reviewer: {C.GREEN}PASS{C.NC}")
    elif verdict == "warn":
        C.warn(f"Reviewer: {C.YELLOW}WARN{C.NC} — see {review_output}")
    elif verdict == "critical":
        C.err(f"Reviewer: CRITICAL — see {review_output}")
    else:
        C.warn(f"Reviewer: could not parse verdict — see {review_output}")
    return verdict
