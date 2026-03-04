---
name: fault-tolerance
order: 27
commit_message_prefix: "fix: "
max_budget_usd: 5.00
max_turns: 20
gate: hard
max_retries: 0
review: true
---

# Fault Tolerance

Fix non-atomic writes, lost updates, missing fsync, and idempotency bugs.

## Why this round exists

Crashes, power failures, and concurrent processes can corrupt state files, lose progress metadata, or produce duplicate side effects. This round hardens file-based state management so that observable state is always consistent — even after an unclean shutdown.

**This round is distinct from the concurrency round.** Concurrency fixes races between threads/goroutines sharing memory. Fault tolerance fixes durability and crash-recovery problems that exist even in single-threaded programs: partial writes, missing fsync, non-idempotent retries, and lost updates from unsynchronized read-modify-write cycles on files.

## Approach

Work through the following areas in order. After each fix, run the test suite to confirm nothing broke.

### A. Atomic writes

Look for direct writes to state, config, or metadata files that leave a window where the file is truncated or partial.

**Flag:**
- Direct overwrites (`open("config.json", "w")` followed by `write()`) without the write-temp-fsync-rename pattern
- Temp files created in `/tmp` when the target file is on a different filesystem (breaks `rename()` atomicity — rename across filesystems is not atomic)
- Predictable temp file names (hardcoded names like `config.json.tmp`) instead of `mkstemp()` / `NamedTemporaryFile`
- Missing cleanup of temp files in error paths (need `finally` block or context manager)

**Fix pattern:** write to a temp file in the same directory → `fsync()` the file → `os.rename()`/`os.replace()` → `fsync()` the parent directory.

### B. Preventing lost updates

Look for read-modify-write cycles on shared state files without locking.

**Flag:**
- Read JSON/YAML → modify in memory → write back, with no lock held across the cycle
- Two processes or threads that could both produce version N+1 from the same version N, with one silently overwriting the other

**Fix (in order of preference):**
1. File advisory locks (`fcntl.flock()` in Python, `syscall.Flock()` in Go) held across the entire read-modify-write cycle
2. Compare-and-swap via file content versioning (embed a version counter, reject stale writes)
3. Threading locks (`threading.Lock`) when access is confined to a single process
4. The lock **must** cover the entire read-modify-write, not just the write

### C. Durability (fsync)

Look for atomic rename patterns that omit fsync.

**Flag:**
- Write-rename without `fsync()`/`fdatasync()` between write and rename — the filesystem may reorder operations, leaving the renamed file with stale or zero-length content after a crash
- Missing `fsync()` on the parent directory after rename (the directory entry must be durable too)
- Unchecked return values from `fsync()` and `write()` — silent failures are common and catastrophic

**Language-specific patterns:**
- **Python:** `os.fsync(f.fileno())` before close, then `os.rename()`, then `os.fsync(os.open(dir, os.O_RDONLY))` on the parent directory
- **Go:** `f.Sync()` before `os.Rename()`
- **Node.js:** `fs.fsyncSync(fd)` before `fs.renameSync()`

### D. Idempotency

Look for operations that produce duplicates or corrupt state when retried.

**Flag:**
- Append-mode writes in contexts that should be idempotent (retrying appends the same data twice)
- `INSERT` without `ON CONFLICT`, `os.mkdir` without `exist_ok=True`
- Counter increments or timestamp mutations that happen unconditionally (should be guarded by "already done" checks)
- Missing deduplication checks before performing side effects

**Fix:** check-before-act patterns, unique IDs for deduplication, progress checkpoints that record what has already been completed.

### E. Testing fault tolerance

Generate targeted tests for the fault tolerance patterns being fixed. **Do not modify existing tests** — only add new fault-tolerance-specific test files or test cases.

**Atomic write tests:**
- Use `pyfakefs` (Python), `fstest.MapFS` (Go), or `mock-fs` (Node.js) to create in-memory filesystems
- Simulate failures at each step: after temp file creation, after write but before fsync, after fsync but before rename
- Verify the original file is intact after simulated failure (not truncated or partial)

**Idempotency tests:**
- Run-twice-assert-once: execute the operation twice, verify state is identical after both runs
- Verify that re-running after a partial execution (simulated by writing a progress checkpoint mid-way) completes correctly

**Lost-update tests:**
- Use `threading.Barrier` (Python) or equivalent to synchronize two writers attempting simultaneous updates
- Verify that exactly one succeeds, or both succeed with serialized results (no data lost)
- Test that the locking mechanism actually blocks concurrent access

**fsync verification tests:**
- Mock `os.fsync` and assert it is called at the right points (after write, before rename)
- Assert the parent directory is fsynced after rename
- Verify that fsync/write return values are checked (mock them to raise `OSError`)

### F. Verify

After each fix, run the full test suite to confirm existing behavior is preserved. Do not proceed to the next area if tests are failing.

## Behavior Contract

### MUST change
- Non-atomic file overwrites (direct writes without write-temp-fsync-rename)
- Read-modify-write cycles on state files without locking
- Missing fsync between write and rename in atomic write patterns
- Non-idempotent operations that corrupt state when retried

### MUST NOT change
- Storage formats or serialization schemes
- File layout or directory structure
- Database schemas or migration files
- Existing test files

## What NOT to do

- **Do not** add fault tolerance to throwaway or ephemeral data (caches, temp files that are regenerated on startup)
- **Do not** introduce database-level transaction machinery for simple file operations
- **Do not** convert file-based state to a database — fix the file operations, don't change the architecture
- **Do not** add retry loops — this round fixes atomicity and durability, not transient failure recovery
- **Do not** over-engineer idempotency for operations that genuinely run only once
- **Do not** modify existing tests — only add new fault-tolerance-specific tests
