"""Entry point for ``python -m quality_pipeline``."""

from __future__ import annotations

import click

from .pipeline import pipeline


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
