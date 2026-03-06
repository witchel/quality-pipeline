"""Git helper functions and worktree setup."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .output import C
from .config import ENV_FILES


def git(*args: str, capture: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command, returning CompletedProcess."""
    return subprocess.run(
        ["git", *args],
        capture_output=capture,
        text=True,
        check=check,
    )


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
    """Commit staged changes.

    Tries --no-gpg-sign first (avoid passphrase prompts in headless pipeline),
    falls back to a plain commit if the flag itself is rejected.
    """
    result = git("commit", "-m", msg, "--no-gpg-sign", check=False)
    if result.returncode == 0:
        return
    # Retry without --no-gpg-sign in case git is too old to support it
    result2 = git("commit", "-m", msg, check=False)
    if result2.returncode == 0:
        return
    stderr = (result2.stderr or result.stderr or "").strip()
    C.err(f"git commit failed: {stderr}")
    raise subprocess.CalledProcessError(result2.returncode, "git commit")


def _lock_pid_path(lock_path: Path) -> Path:
    """Return the sibling PID file path for a lock directory."""
    return lock_path.parent / f"{lock_path.name}.pid"


def _is_lock_stale(lock_path: Path) -> bool:
    """Check if a lock directory holds a stale (dead process) lock.

    Reads the sibling PID file and checks whether that process is alive.
    Returns False (conservative) when the PID file is missing or unreadable.
    """
    pid_file = _lock_pid_path(lock_path)
    try:
        old_pid = int(pid_file.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return False  # No PID file → can't determine, assume live
    try:
        os.kill(old_pid, 0)
    except ProcessLookupError:
        return True  # Process dead → stale
    except (PermissionError, OSError):
        pass  # Process exists → not stale
    return False


def git_acquire_lock(dry_run: bool) -> Path | None:
    """Acquire a lock to prevent concurrent pipeline runs.

    Writes a sibling PID file so future runs can detect and reclaim
    stale locks left by crashed processes (e.g. SIGKILL).
    """
    if dry_run:
        return None
    git_dir = git("rev-parse", "--git-dir").stdout.strip()
    lock_path = Path(git_dir) / "quality-pipeline.lock"
    pid_file = _lock_pid_path(lock_path)
    try:
        lock_path.mkdir()
    except FileExistsError:
        if _is_lock_stale(lock_path):
            C.warn("Stale pipeline lock detected — reclaiming")
            try:
                pid_file.unlink(missing_ok=True)
                lock_path.rmdir()
                lock_path.mkdir()
            except OSError as e:
                C.err(f"Failed to reclaim stale lock: {e}")
                C.err(f"Manual cleanup: rm -rf '{lock_path}' '{pid_file}'")
                sys.exit(1)
        else:
            C.err("Another pipeline is running in this repository.")
            C.err(f"If stale, remove: rmdir '{lock_path}'")
            sys.exit(1)
    # Record PID for stale lock detection
    pid_file.write_text(str(os.getpid()))
    return lock_path


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
