# Quality Pipeline

A reusable, multi-round automated code quality tool for Claude Code. Runs sequential quality rounds (testing, refactoring, concurrency, fault tolerance, error handling, security, type safety, dead code elimination, dependency hygiene, simplification) with test verification and clean git commits.

## Installation

```bash
cd ~/.claude/plugins/
git clone <repo-url> quality-pipeline
```

The `/quality-pipeline` slash command is now available in any Claude Code session.

## Usage

### Interactive (Claude Code session)

```
/quality-pipeline
/quality-pipeline --rounds "audit-tests dead-code"
/quality-pipeline --start-from 3
/quality-pipeline --dry-run
```

### Headless (terminal)

```bash
uv run ~/.claude/plugins/quality-pipeline/scripts/quality_pipeline.py \
    --rounds "audit-tests refactor concurrency dead-code simplify" \
    --test-command "npm test"
```

## Options

| Option | Description |
|--------|-------------|
| `--project-dir DIR` | Run in DIR instead of current directory |
| `--rounds "r1 r2 ..."` | Which rounds to run (default: all) |
| `--config FILE` | Path to pipeline.yaml config |
| `--start-from N` | Resume from round N (1-indexed) |
| `--dry-run` | Show plan without executing |
| `--worktree` | Run in an isolated git worktree (safe with uncommitted changes) |
| `--worktree-symlinks "d1 d2"` | Space-separated dirs to symlink into worktree |
| `--test-command "CMD"` | Override auto-detected test command |
| `--review` | Enable reviewer pass for all rounds |
| `--no-review` | Disable reviewer pass for all rounds |
| `--log-dir DIR` | Directory for log files |

## Built-in Rounds

| Round | Prefix | Budget | Gate | Retries | Review | Description |
|-------|--------|--------|------|---------|--------|-------------|
| `audit-tests` | `test:` | $5.00 | hard | 2 | | Audit test quality and fill coverage gaps with substantial, independent tests |
| `refactor` | `refactor:` | $5.00 | soft | 1 | | Improve naming, structure, and clarity |
| `concurrency` | `fix:` | $5.00 | hard | 0 | yes | Fix races, lost updates, and find parallelization opportunities |
| `fault-tolerance` | `fix:` | $5.00 | hard | 0 | yes | Fix non-atomic writes, lost updates, missing fsync, and idempotency bugs |
| `error-handling` | `fix:` | $5.00 | hard | 1 | | Fix swallowed errors, missing error paths, and inconsistent patterns |
| `security` | `fix:` | $5.00 | hard | 0 | yes | Fix hardcoded secrets, injection vectors, and insecure defaults |
| `type-safety` | `refactor:` | $5.00 | soft | 1 | | Add missing type annotations and tighten overly broad types |
| `dead-code` | `chore:` | $3.00 | soft | 1 | | Remove unused imports, functions, and variables |
| `dependency-hygiene` | `chore:` | $3.00 | soft | 1 | | Remove unused dependencies and flag deprecated API usage |
| `simplify` | `style:` | $3.00 | soft | 1 | | Reduce unnecessary abstractions and complexity |

## How It Works

1. Creates a branch `quality/YYYY-MM-DD-<hash>` (optionally in an isolated worktree with `--worktree`)
2. For each round:
   a. Runs static analysis tools (if configured) and injects results into the prompt
   b. Invokes `claude -p` with a focused prompt
   c. Runs your test suite to verify nothing broke
   d. On test failure: retries with test output (if `max_retries > 0`)
   e. On final failure: rolls back and applies gate logic (hard=stop, soft=continue)
   f. On success: commits and optionally runs a reviewer pass
3. Moves to the next round

Rounds interact exclusively through git state — each round starts a fresh Claude session that sees the updated codebase.

## Gate Types

Each round has a `gate` setting that controls what happens when tests fail:

- **hard** (default): Pipeline stops. Use for correctness-critical rounds (tests, concurrency, security).
- **soft**: Pipeline continues to the next round. Use for best-effort rounds (refactoring, dead code, simplify).
- **none**: Tests are skipped entirely. Use for rounds that don't affect behavior.

## Retry Loop

When tests fail after a round, the pipeline can retry by re-invoking Claude with the test output. Set `max_retries` in the round frontmatter (default: 0). Each retry uses half the round's budget and shows Claude the last 100 lines of test output.

## Static Analysis

Rounds can be augmented with static analysis results. The pipeline runs configured analyzers before Claude and injects their output into the prompt. Default mappings:

- `security` → bandit, semgrep
- `type-safety` → mypy, pyright, tsc
- `dead-code` → vulture

Override per-round with the `analyzers` frontmatter field or via `overrides` in pipeline.yaml.

## Behavior Contracts

Correctness-critical rounds (concurrency, fault-tolerance, error-handling, security) include Behavior Contract sections that specify what MUST change and what MUST NOT change. These constrain Claude's changes and give the reviewer something concrete to check against.

## Reviewer Pass

After a round commits, an optional reviewer pass invokes a fresh Claude session to review the diff. The reviewer checks for correctness, contract compliance, scope creep, test quality, and subtle regressions. Enable per-round with `review: true` in frontmatter, or globally with `--review`.

## Per-Project Configuration

Drop a `.claude/pipeline.yaml` in your project:

```yaml
test_command: "pytest tests/"
rounds: [audit-tests, refactor, concurrency, fault-tolerance, error-handling, security, type-safety, dead-code, dependency-hygiene, simplify]
branch_prefix: "quality/"
max_budget_usd: 20.00
overrides:
  audit-tests:
    max_budget_usd: 8.00
    append_prompt: "Use pytest with fixtures"
  dead-code:
    gate: none
    max_retries: 0
  security:
    review: true
    analyzers: "bandit semgrep"
```

## Custom Rounds

Add a markdown file to `rounds/` with YAML frontmatter:

```markdown
---
name: my-round
commit_message_prefix: "feat: "
max_budget_usd: 5.00
max_turns: 20
gate: hard
max_retries: 1
review: false
analyzers: "mypy pyright"
---

# My Custom Round

Your prompt here...
```

Frontmatter fields (all optional, with backward-compatible defaults):

| Field | Default | Description |
|-------|---------|-------------|
| `name` | `""` | Round identifier (used for config overrides and display) |
| `commit_message_prefix` | `"chore: "` | Git commit message prefix |
| `max_budget_usd` | `5.00` | Claude API budget cap |
| `max_turns` | `20` | Maximum Claude conversation turns |
| `gate` | `"hard"` | `hard`, `soft`, or `none` (see Gate Types) |
| `max_retries` | `0` | Retry attempts on test failure |
| `review` | `false` | Run a reviewer pass after commit |
| `analyzers` | `""` | Space-separated static analysis tools to run |

## Test Command Detection

The pipeline auto-detects your test runner by checking (in order):

1. CLAUDE.md for test command mentions
2. Makefile `test` target
3. package.json `test` script (respects bun/pnpm/yarn lockfiles)
4. pyproject.toml / pytest configuration (uses `uv run pytest` if `uv.lock` present)
5. go.mod → `go test ./...`
6. Cargo.toml → `cargo test`

Override with `--test-command` or `test_command` in pipeline.yaml.

## Failure Recovery

- If a round fails, previous rounds' commits are preserved on the branch
- Resume with `--start-from N` to skip completed rounds
- **Hard gate** rounds: test failure rolls back changes and stops the pipeline
- **Soft gate** rounds: test failure rolls back changes but continues to the next round
- Retry loop (if configured) re-invokes Claude with test output before giving up
