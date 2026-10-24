# Compaction Publication

Fork-Patch: compaction-publication

## Contract

A successful summary and its covered-history snapshot stay together through bounded
publication retries. Only rolled-back SQLite lock contention is retryable.
Lease loss, generation conflicts, deliberate session closure, and other failures
keep the original transcript. Publication failure is not a summarizer failure and
must not select another summary model.

Writer patience includes acquisition of the SessionDB instance mutex, not only
SQLite's `BEGIN IMMEDIATE`. Compression lease timestamps must be calculated after
that wait, inside the write callback. Atomic publication, lease ownership, covered
row checks, FTS triggers, display reconciliation, and rewind flags stay intact.

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

## Verification And Removal

Regression coverage belongs in `tests/hermes_state/test_publication_wait_budget.py`
and `tests/agent/test_compaction_events.py`, with the surrounding state,
compaction, rollback, display, FTS, and lease suites. Run them with
`scripts/run_tests.sh`, then the exact-head `bin/ci gate` through `heavy-slot`.

Upstream main `5894e750c3dba980d43b25aa29e3958149ba2c61` and relevant open PRs
were checked. This patch is removable when upstream writer patience covers the
local mutex, refresh timestamps are written fresh, publication-only retries reuse
the generated candidate, and generation fallback excludes publication failures.
It complements fork PR #752's transcript phase timing rather than replacing it.
