# Quality Pipeline

A reusable, multi-round automated code quality tool for Claude Code. Runs sequential quality rounds (testing, refactoring, concurrency safety, dead code elimination, simplification) with test verification and clean git commits.

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
/quality-pipeline --rounds "add-tests dead-code"
/quality-pipeline --start-from 3
/quality-pipeline --dry-run
```

### Headless (terminal)

```bash
~/.claude/plugins/quality-pipeline/scripts/quality-pipeline.sh \
    --rounds "add-tests refactor concurrency dead-code simplify" \
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
| `--test-command "CMD"` | Override auto-detected test command |

## Built-in Rounds

| Round | Prefix | Budget | Description |
|-------|--------|--------|-------------|
| `add-tests` | `test:` | $5.00 | Add comprehensive tests for undertested code |
| `refactor` | `refactor:` | $5.00 | Improve naming, structure, and clarity |
| `concurrency` | `fix:` | $5.00 | Fix data races, missing synchronization, and concurrency bugs |
| `dead-code` | `chore:` | $3.00 | Remove unused imports, functions, and variables |
| `simplify` | `style:` | $3.00 | Reduce unnecessary abstractions and complexity |

## How It Works

1. Creates a branch `quality/YYYY-MM-DD-<hash>`
2. For each round, invokes `claude -p` with a focused prompt
3. Runs your test suite to verify nothing broke
4. Creates a clean git commit with a conventional commit prefix
5. Moves to the next round

Rounds interact exclusively through git state — each round starts a fresh Claude session that sees the updated codebase.

## Per-Project Configuration

Drop a `.claude/pipeline.yaml` in your project:

```yaml
test_command: "pytest tests/"
rounds: [add-tests, refactor, concurrency, dead-code, simplify]
branch_prefix: "quality/"
max_budget_usd: 20.00
overrides:
  add-tests:
    max_budget_usd: 8.00
    append_prompt: "Use pytest with fixtures"
```

## Custom Rounds

Add a markdown file to `rounds/` with YAML frontmatter:

```markdown
---
name: my-round
order: 25
commit_message_prefix: "feat: "
max_budget_usd: 5.00
max_turns: 20
---

# My Custom Round

Your prompt here...
```

## Test Command Detection

The pipeline auto-detects your test runner by checking (in order):

1. CLAUDE.md for test command mentions
2. Makefile `test` target
3. package.json `test` script (respects bun/pnpm/yarn lockfiles)
4. pyproject.toml / pytest configuration
5. go.mod → `go test ./...`
6. Cargo.toml → `cargo test`

Override with `--test-command` or `test_command` in pipeline.yaml.

## Failure Recovery

- If a round fails, previous rounds' commits are preserved on the branch
- Resume with `--start-from N` to skip completed rounds
- If tests fail after a round, changes are rolled back and the pipeline stops
