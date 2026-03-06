# Quality Pipeline

Multi-round automated code quality tool for Claude Code.

## Run tests

```
uv run pytest tests/ -x -q
```

## Run the pipeline on itself

```bash
# Preview
uv run quality-pipeline --worktree --dry-run

# Full run
uv run quality-pipeline --worktree

# Cherry-pick rounds
uv run quality-pipeline --worktree --rounds "audit-tests simplify"
```

`--worktree` is required for self-runs: the pipeline modifies source files via
`claude -p`, then runs tests that import those same modules.

## Project structure

- `quality_pipeline/` — Python package (9 modules + __init__.py)
- `quality_pipeline/rounds/` — built-in round definitions (YAML frontmatter + prompt)
- `quality_pipeline/templates/` — reviewer template
- `tests/` — pytest suite (206 tests, split per-module)
