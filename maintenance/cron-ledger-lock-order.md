# Cron execution-ledger lock order

Load this unit when changing due scans or external fire acquisition.

Fork patch identity: `cron-ledger-lock-order`.

## Required behavior

Avoid ledger I/O for future slots and prefetch monotonic completion/owner facts
outside the jobs flock. Only positive completion is reusable: a prefetched False
can go stale when a runner publishes completion after preflight, so genuinely due
or unresolved slots retain base's exact locked lookup. This deliberately narrows
the critical section rather than claiming that every ledger operation is removed.
A fixture with 200 future and two due jobs measures 202 locked reads at base and
two at head, with identical due output.

Current claims are still rechecked under the jobs lock and per-job fire fence.
Proven owner death may release a claim; live/unknown owners conservatively retain
the claim until normal expiry. Facts are call/profile/thread-local, never a global
or TTL cache. Do not introduce speculative mutations or CAS commits: notices,
tombstones, missed-window receipts, repairs, claims, saves, and recovery-window
changes retain their locked base ordering. Malformed UTC conversion during
preflight must not escape base's per-job repair/containment.

Keep exact completed-occurrence deduplication after an independent jobs.json
rollback (the intent of `7d6d0709cdcb`), manual-fire exemption, future-slot skew
protection, one-shot claims, pending slots, and save-failure containment. No new
runtime setting, lock ordering, cross-process fence, or lock-timeout fallback.

## Verification

Contract tests: `tests/cron/test_jobs_ledger_contention.py`,
`tests/cron/test_jobs_prefetch_freshness.py`, `tests/cron/test_scheduled_occurrence.py`,
`tests/cron/test_jobs_crossprocess_lock.py`, `tests/cron/test_due_scan_save_failure.py`.

Run these through `scripts/run_tests.sh`, then the full `tests/cron/` suite.
The contention regression blocks preflight while an independent interpreter
pauses using the real advisory lock with a one-second bound; both scan and fire
claim must leave it available and respect the current paused record. A stale
negative completion race proves the exact locked lookup rejects a restored slot.
A differential on identical expired/missed-window/outage fixtures compares the
prefetched public gate with the base locked gate: notices, saves, coverage, and
resulting records must match, and every mutation remains under the jobs lock.

## Upstream and retirement

Upstream issue NousResearch/hermes-agent#133883 and PR#134532 track redundant
ledger connections in due scans. Sharing a connection inside the jobs flock is
not equivalent to moving preflight/owner probes and excluding future-slot I/O.
Retire when upstream supplies equivalent contention reduction while retaining
fresh due-negative dedup and locked mutation ordering. Revert the commit carrying
`Fork-Patch: cron-ledger-lock-order` to roll back.

This is source-only work. Passing tests/opening a PR does not update a running
gateway, eliminate contention for genuinely due jobs, or guarantee tonight's
on-time starts on the unmodified older release.
