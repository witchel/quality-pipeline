# /quality-pipeline

Run the automated multi-round code quality pipeline.

## Usage

```
/quality-pipeline [options]
```

## Options

- `--rounds "round1 round2 ..."` — Specify which rounds to run (default: all rounds in `rounds/` directory)
- `--config path/to/pipeline.yaml` — Use a custom pipeline config file
- `--start-from N` — Resume pipeline from round N (1-indexed)
- `--dry-run` — Show what would happen without running anything
- `--test-command "cmd"` — Override auto-detected test command
- `--review` — Enable reviewer pass for all rounds
- `--no-review` — Disable reviewer pass for all rounds

## What This Does

This pipeline runs N sequential quality improvement rounds on your codebase. Each round:

1. Invokes `claude -p` with a focused prompt for that round's objective
2. Runs your test suite to verify nothing broke
3. Creates a clean git commit with a conventional commit prefix

Rounds interact exclusively through git state — each round starts a fresh Claude session that reads the updated codebase.

## Instructions

Run the quality pipeline orchestration script. Execute it like this:

```bash
uv run "$PLUGIN_DIR/scripts/quality_pipeline.py" $ARGUMENTS
```

Where `$PLUGIN_DIR` is the directory containing this plugin (the parent of `commands/`) and `$ARGUMENTS` are the arguments the user passed to this command.

If the user passed no arguments, run with default settings (all rounds).

**Important**: Before running, make sure the working directory has no uncommitted changes. If there are uncommitted changes, warn the user and ask if they want to proceed.
