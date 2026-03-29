---
name: concurrency
commit_message_prefix: "fix: "
max_budget_usd: 5.00
max_turns: 30
max_time_minutes: 25
gate: hard
max_retries: 0
review: true
review_gate: soft
---

# Fix Concurrency Bugs and Find Parallelization Opportunities

You are a concurrency specialist. Your goal is to fix data races, race conditions, missing synchronization, and lost updates — and to identify sequential code that would benefit from parallelization.

## Pre-check

Before making any changes, scan the codebase for concurrency primitives (threads, async, goroutines, locks, channels, worker pools, etc.). If the project is single-threaded with no concurrent patterns, output a short message saying "No concurrency patterns found — nothing to fix" and stop. Do not add concurrency where none exists.

## Approach

1. **Identify concurrent code**: Look for language-specific concurrency patterns:
   - **Go**: goroutines (`go func`), channels, `sync` package usage
   - **Python**: `threading`, `multiprocessing`, `asyncio`, `concurrent.futures`
   - **Java**: `Thread`, `Runnable`, `ExecutorService`, `synchronized`, `java.util.concurrent`
   - **JavaScript/TypeScript**: `async/await`, `Promise.all`, Web Workers, shared state across callbacks
   - **Rust**: `Arc`, `Mutex`, `RwLock`, `tokio::spawn`, `std::thread::spawn`

2. **Check for missing synchronization**:
   - **Unprotected shared writes**: Multiple goroutines/threads writing to the same variable without a lock or atomic
   - **Read-write races**: One thread reads while another writes without synchronization
   - **TOCTOU (time-of-check-to-time-of-use)**: Checking a condition and acting on it non-atomically (e.g., checking file existence then opening)
   - **Unsafe compound operations**: Non-atomic read-modify-write sequences on shared state (e.g., `counter++` without a lock)
   - **Lost updates**: Two writers read the same state, both modify it, and the second write silently overwrites the first (e.g., read-modify-write on a JSON file without locking, concurrent map updates where one update is lost). Fix with compare-and-swap, file locking, or serialized access.

3. **Fix with minimal correct primitives**:
   - Prefer atomics over mutexes when a single variable is involved
   - Keep critical sections as narrow as possible
   - Use consistent lock ordering to prevent deadlocks
   - Prefer channel-based communication over shared memory where idiomatic (e.g., Go)
   - Use `sync.Once`, `sync.Map`, or equivalent when appropriate
   - For file-based state, use advisory locks (`flock`, `fcntl`) or atomic write-rename patterns

4. **Check for structural concurrency issues**:
   - **Goroutine/thread leaks**: Goroutines or threads that can never terminate (e.g., blocked on a channel nobody closes, missing context cancellation)
   - **Missing context propagation**: Long-running operations that don't respect cancellation
   - **Dropped errors from concurrent operations**: Errors in goroutines/threads that are silently ignored
   - **Unbounded concurrency**: Spawning goroutines/threads in a loop without a semaphore or pool limit

5. **Find parallelization opportunities**: Look for sequential operations that are independent and could run concurrently:
   - Multiple independent I/O operations (API calls, file reads, network requests) executed one after another
   - Processing items in a loop where each iteration is independent
   - Sequential steps that have no data dependency between them
   - Use idiomatic patterns: `asyncio.gather()` / `asyncio.TaskGroup` in Python, `Promise.all()` in JS, `sync.WaitGroup` / `errgroup` in Go, `CompletableFuture.allOf()` in Java
   - Only parallelize when the operations are genuinely independent and the overhead is justified (don't parallelize two 1ms operations)
   - **After parallelizing, trace the full data flow**: verify that return types from worker functions match what the caller destructures, that result aggregation logic is correct, and that any counters or accumulators are updated correctly. Parallelization changes function boundaries — a sequential loop's inline variables become a worker function's return values, and mismatches (e.g., returning a 3-tuple but destructuring as 2-tuple) cause runtime crashes that tests may not cover

6. **Verify**: Run the test suite after each fix to confirm behavior is preserved. For newly-parallelized code, also manually trace the worker function's return type through to the caller's destructuring — this is the most common source of bugs in sequential-to-parallel conversions.

## Behavior Contract

### MUST change
- Unprotected shared writes (multiple goroutines/threads writing without a lock or atomic)
- Read-modify-write sequences on shared state without synchronization
- TOCTOU patterns on shared resources without atomic operations
- Missing context propagation in long-running concurrent operations

### MUST NOT change
- Single-threaded code paths (do not add locks where no concurrent access exists)
- The concurrency model itself (do not convert threads to async, goroutines to channels, etc.)
- Existing test files
- Public API signatures or return types

## What NOT to do

- Don't add locks or synchronization to single-threaded code
- Don't restructure the concurrency model (e.g., converting threads to async or goroutines to channels) — fix races within the existing model
- Don't add defensive "just in case" locks where no concurrent access exists
- Don't fix concurrency performance issues (lock contention, false sharing) — focus on correctness and obvious parallelization wins
- Don't modify tests
- Don't parallelize operations that have side effects on shared state unless you also add proper synchronization
- Don't parallelize a sequential loop without verifying the entire result-handling path still works. Common bugs: worker returns a different tuple shape than the caller expects, counters that relied on sequential ordering now race, and progress reporting that assumed items complete in order
- Don't parallelize code paths that lack test coverage for the parallel case. If existing tests only exercise the sequential path, the parallelization is untested. Add a test that exercises the parallel execution (even a simple one with 2-3 items) before considering the change complete
