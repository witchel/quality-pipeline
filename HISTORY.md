# Quality Pipeline — History

A curated log of significant changes to the quality pipeline plugin.

---

## 2026-03-04 — Rewrite pipeline from shell to Python

The quality pipeline was originally three shell scripts: `quality-pipeline.sh`
(1156 lines), `detect-test-command.sh` (117 lines), and
`run-static-analysis.sh` (105 lines). Shell was a poor fit for this task —
YAML frontmatter parsing required awk hacks, JSON parsing for reviewer
verdicts shelled out to Python, config overrides used dynamic variable names
via `eval`, and the `PIPESTATUS` trick for capturing test exit codes through
`tee` was fragile. The shell scripts already had a hidden Python dependency
(they shelled out to `python3` for YAML parsing, JSON parsing, and template
substitution), so the "no dependencies" argument for shell didn't hold.

Consolidated everything into a single `scripts/quality_pipeline.py` (~780
lines) using PEP 723 inline script metadata, so `uv run
scripts/quality_pipeline.py` auto-installs `click` and `pyyaml` with zero
setup. Key improvements:

- **YAML/JSON parsing is native** — `yaml.safe_load` for frontmatter,
  `json.loads` for reviewer verdicts, `json.load` for package.json. No more
  awk/sed/grep chains.
- **Config overrides are dictionary lookups** — replaced the shell pattern of
  `eval`-ing dynamic variable names like `CONFIG_OVERRIDE_${SAFE}_BUDGET`,
  which was a shell-injection surface.
- **Test output capture uses `subprocess.Popen`** with manual line-by-line tee,
  giving the real exit code directly instead of the `PIPESTATUS[0]` trick.
- **Resource monitor is a daemon `threading.Thread`** with `Event`-based stop,
  replacing the background subshell + `kill`/`wait` dance.
- **Click handles CLI parsing** — `--review/--no-review` gives
  `True`/`False`/`None` natively, `--start-from` gets integer validation for
  free.

---

## 2026-03-04 — Code quality pass on the Python pipeline

Several improvements to the Python script, all verified against the existing
58-test suite:

- **`apply_config_overrides` no longer mutates its input** — previously it
  modified the `RoundConfig` in-place *and* returned it, a classic Python
  footgun. Now uses `dataclasses.replace()` to create a shallow copy before
  applying overrides.
- **Extracted `_parse_verdict()`** from `run_reviewer` — the deeply nested
  try/except JSON parsing block (handling `{"result": ...}` wrappers and
  markdown code fences) is now a focused, independently testable function.
- **DRY round header printing** — the identical 5-line header block was
  duplicated between `run_round` and the dry-run path in `pipeline()`.
  Extracted to `_print_round_header()`.
- **Type annotations modernized** — replaced all `Optional[X]` with `X | None`
  (the file already imports `from __future__ import annotations`). Fixed pyright
  type errors with `DEFAULT_SYMLINK_DIRS` list literal.
- **Defensive `_cleanup_worktree`** — removed the `or Path("/")` fallback that
  would `os.chdir("/")` if `original_dir` was never set. Now only chdir when
  there's an actual original directory to return to.
- **README updated** — added missing `--worktree`, `--worktree-symlinks`, and
  `--log-dir` options. Replaced reference to deleted `quality-pipeline.sh`.
  Replaced stale `CONFIG_OVERRIDE_<NAME>_ANALYZERS` reference. Added frontmatter
  field reference table.

---

## 2026-03-05 — Sentinel None for config fields and cleanup robustness

The config override system had a subtle design flaw: `RoundConfig` used concrete
defaults (e.g., `max_budget_usd: float = 5.00`), which meant there was no way to
distinguish "the frontmatter explicitly set this to $5" from "the frontmatter
didn't set this at all." A global `max_budget_usd: 20.00` in pipeline.yaml would
silently overwrite a frontmatter-explicit `max_budget_usd: 5.00`, since both
looked the same to the override logic.

Fixed by changing `max_budget_usd`, `max_turns`, and `max_time_minutes` to
`None`-sentinel fields. A new `_finalize_round_config()` fills in real defaults
after all overrides are applied. The priority chain is now unambiguous:
per-round override > frontmatter > global config > default.

`_finalize_round_config` also validates gate values — a typo like `gate: hardd`
now warns and defaults to `"hard"` instead of silently behaving as hard in some
code paths and falling through in others.

The `run_round` function was split into a thin wrapper (try/finally to guarantee
`_cleanup.current_round` is cleared) and `_execute_round` (the body). Previously
every exit path had to remember to clear `current_round` — five copies of the
same line, and any exception escaping would skip them all, leaving a confusing
"Interrupted during round: X" message in the cleanup handler.
