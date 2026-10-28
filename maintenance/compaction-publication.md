# Compaction Publication

Fork patch identity: `compaction-publication`

## Contract

A successful summary and its covered-history snapshot stay together through bounded
publication retries. Only rolled-back SQLite lock contention is retryable.
Lease loss, generation conflicts, deliberate session closure, and other failures
keep the original transcript. Publication failure is not a summarizer failure and
must not select another summary model.

Commit admission alone is not a publication failure: an admitted worker that
finishes unchanged at its deadline may still recover on the next model route,
after the first worker has fully settled. The fence records actual publication
failure separately so storage errors cannot enter that recovery path.

Compaction publication patience includes acquisition of the SessionDB instance
mutex, not only SQLite's `BEGIN IMMEDIATE`. Ordinary writers retain their existing
unbounded mutex admission and SQLite retry budgets, so a long local holder cannot
turn a queued transcript append into a failed write. Publication mutex timeouts
log holder diagnostics before propagating and are terminal to both retry layers.

Every successful `LoopGuardedLock` acquisition records a monotonic timestamp,
thread identity, and 3 code/line pairs, without retaining frames or logging on
acquire. A bounded local timeout logs that owner and held duration before the
existing SQLite file-lock diagnostic. The acquisition counter delta reports new
acquisitions during the wait, not distinct threads. The snapshot is best-effort
at acquire/release handoff and does not prove a single holder covered the wait.
Slow transaction warnings also trigger on acquisition delay alone, keeping the
reported acquire and hold durations separate.

The 2026-10-26 6:57:18 PM PT local mutex timeout exhausted approximately 20 seconds
of publication patience without identifying its holder. The retained diagnosis
found no owner metadata, a file-lock-only timeout diagnostic unavailable on
Darwin, and a slow-write threshold that excluded acquisition time. Those are
observability gaps, not proof of the historical holder or cause.

Publication accounting is scoped to the publication retry call, not the whole
commit phase. Memory extraction, rotation flushes, title and prompt updates, and
pre-publication preparation do not inherit its bounded-lock context or inflate
its metrics. Memory-provider background threads therefore cannot retain and
mutate a completed attempt's metrics. Compression lease timestamps must be
calculated after the wait, inside the write callback. Atomic publication, lease
ownership, covered row checks, FTS triggers, display reconciliation, and rewind
flags stay intact.

The publication scope accumulates total SQLite changes, including trigger work
and rolled-back attempts, across retries. Attempt-local write diagnostics use a
separate ContextVar, reset for each admitted transaction, to report logical row
counts and callback/commit/rollback phases. Neither scope contributes its counts
to the other, and both restore any enclosing context on success or failure.

## Incident Evidence

The 2026-10-23 incident's 3 attempts spent 634497, 603812, and 555910 ms in
publication before rejecting an expired/lost compression lease. The lease check
is the first operation in the archive transaction, before transcript copying.
No slow write-lock hold warning accompanied those failures. A concurrent session
search completed in 549019 ms immediately before all 3 failures.

The old writer retry deadline is checked only after acquiring the unbounded
Python mutex. This permits a 10-minute mutex wait even with a short SQLite busy
budget, and a lease refresher blocked on the same mutex can install an already
expired deadline because it calculated its timestamp before waiting. This is a
reproducible stall mechanism, not conclusive attribution of every incident
millisecond. A long display-plan read or reader-pool wait is another possible
pre-transaction contributor. Separate publication wait/hold timings are required
to distinguish the next occurrence.

The clean-close race test's lock observer implements both context-manager and
timed acquire/release entry, and observes both cached and full generation guards.
This preserves its teardown barrier when writer admission or guard selection
changes without relaxing the clean-close/sticky-loss invariant.

## Verification And Removal

Regression coverage belongs in `tests/hermes_state/test_publication_wait_budget.py`
and `tests/agent/test_compaction_events.py`, with the surrounding state,
compaction, rollback, display, FTS, and lease suites. Run them with
`scripts/run_tests.sh`, then the exact-head `bin/ci gate` through `heavy-slot`.

Upstream main `5894e750c3dba980d43b25aa29e3958149ba2c61` and relevant open PRs
were checked. The follow-up checked guidance at upstream main
`bac34e9e9946c9c6a9ce7ce64efadd47f8fa375b` and related closed
[PR #120821](https://github.com/NousResearch/hermes-agent/pull/120821), which addresses
generation fencing rather than local mutex admission. The existing atomic
publication and generation guards remain the owners of persistence correctness.
This patch is removable when upstream compaction publication patience covers the
local mutex without failing ordinary queued writers, refresh timestamps are
written fresh, publication-only retries reuse
the generated candidate, and generation fallback excludes publication failures.
It complements fork PR #752's transcript phase timing rather than replacing it.
Owner-attribution guidance was checked at upstream main
`3f058ad5931828515ff439ab99b5d382e60fda32`. Related open
[PR #139984](https://github.com/NousResearch/hermes-agent/pull/139984) changes
lease renewal admission, while closed
[PR #64918](https://github.com/NousResearch/hermes-agent/pull/64918) combines
retry changes with slow-write diagnostics. Neither supplies the fork's bounded
publication mutex attribution. Extend the existing wrapper and warning instead
of changing admission, patience, or retries.
