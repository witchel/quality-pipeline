"""Pipeline cleanup manager and signal handling."""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .output import C
from .config import ENV_FILES

if TYPE_CHECKING:
    import types

    from .monitoring import ResourceMonitor


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
        self._activated: bool = False

    def activate(self) -> None:
        """Register atexit and signal handlers. Call once from the CLI entry point."""
        if self._activated:
            return
        self._activated = True
        atexit.register(self.cleanup)
        try:
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
            if hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP, _handle_signal)
        except ValueError:
            pass  # not main thread

    def make_temp(self) -> Path:
        fd, path = tempfile.mkstemp()
        os.close(fd)
        p = Path(path)
        self.temp_files.append(p)
        return p

    def cleanup(self) -> None:
        # Block signals so cleanup can't be interrupted mid-way.
        # Guard: signal.signal() must be called from the main thread and the
        # signal module may be partially torn down during interpreter shutdown.
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP, signal.SIG_IGN)
        except (ValueError, OSError, TypeError, AttributeError):
            pass

        if self.monitor and hasattr(self.monitor, "stop"):
            self.monitor.stop()
        for f in self.temp_files:
            f.unlink(missing_ok=True)
        self._cleanup_worktree()
        if self.lock_dir and self.lock_dir.exists():
            try:
                self.lock_dir.rmdir()
            except OSError:
                pass
            # Remove sibling PID file used for stale lock detection
            pid_file = self.lock_dir.parent / f"{self.lock_dir.name}.pid"
            pid_file.unlink(missing_ok=True)
        # Save and clear current_round so repeated calls (signal + atexit)
        # don't print the interruption message twice.
        interrupted_round = self.current_round
        self.current_round = ""
        if interrupted_round:
            print()
            C.err(f"Interrupted during round: {interrupted_round}")
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
            try:
                os.chdir(self.original_dir)
            except OSError:
                pass  # dir may be gone; continue cleanup to remove worktree+lock

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


def _handle_signal(signum: int, _frame: types.FrameType | None) -> None:
    sys.exit(128 + signum)
