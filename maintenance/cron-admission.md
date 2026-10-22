# Cron Admission

**Patch identity:** `cron-protected-admission`.

This unit extends the [fork contract](../MAINTENANCE.md). Load it for cron
completion rearming, built-in ticker waits, or executor admission changes.
Also preserve the process-exit ownership contract in
[cron restart survival](cron-restart-survival.md).

## Preserve

Interval schedules of at most 120 seconds form the protected class, derived
from schedule data rather than names, IDs, or job flags. Their completion-based
next eligibility spends 30 seconds of headroom before the natural interval.
An `every 1m` job therefore rearms 30 seconds after completion and runs more
often than once per minute.
All other schedules retain their existing rearm calculation.

Each profile's existing FIFO general executor keeps `cron.max_parallel_jobs`
(default 4). Add exactly 1 reserved worker, select the first enabled, unpaused,
nonterminal protected job in store order, and send it to the reserved executor.
If selection changes while the previous owner still holds the reservation,
treat the replacement as overflow in the general pool until the slot is free.
Additional protected jobs log an error once per loaded class and use the
general executor best-effort in its unchanged FIFO order. A long run holding
the reservation must not occupy a general worker or overlap itself.

A finished waiter future or terminal execution row does not release the
reservation while its detached process remains live. Only the existing
owner-fenced running-job release returns it. The healthy built-in ticker
waits to the earliest eligible deadline, bounded by its 60-second heartbeat,
and checks worker-release wakes within 0.25 seconds. Due-time and release wakes
add scans without shifting the fixed-period heartbeat deadline. An eligible
non-running job whose deadline crossed after an ungated due scan gets a
1-second rescan rather than waiting for the heartbeat. Already-due jobs left
intact by ESTOP, drain, an unwritable store, tick-lock contention, or a live
cross-process claim retain heartbeat cadence. Scan timing is scoped to each
home and consumed once per ticker cycle. Failed dispatch cleanup does not
signal a worker release wake. Overruns rebaseline instead of bursting. Retain
fd-exhaustion backoff and shutdown responsiveness.

## Update

This is a fork scheduling policy, not an upstream release-baseline change.
Its implementation belongs in `cron/scheduler_admission.py` and the existing
job-run rearm helper, with narrow hooks at the scheduler and provider seams.
Do not replace it with a higher general cap, a second reservation, job-specific
identity matching, or an execution-terminal release shortcut. Retire it only
when the selected upstream baseline preserves the same admission and timing
contract under general-pool saturation.

## Verify

`tests/cron/test_reserved_admission.py` exercises actual `started_at` ledger
stamps against a fake clock with all 4 general workers blocked, 2 protected
jobs, FIFO overflow, long runs, completion rearming, per-job exclusion,
profile deadlines, and both terminal-row and waiter-error process-exit fences.
`tests/cron/test_admission_wakes.py` checks periodic phase, early overruns,
and failure backoff in both ticker modes, plus zero-wait stop observation.
Also run ticker drift, virtual-clock soak, parallel-pool, scheduler-provider,
restart-safe-worker, registration-ownership, cancellation, and multiplex-release
regressions.
Run immediate-run tool tests separately. Source qualification is not live
acceptance: after separately authorized activation, verify the protected job's
actual worker start is at most 120 seconds after its preceding `finished_at`.

Rollback reverts this unit's admission and rearm hooks without weakening the
existing detached-process fence. Do not change live config to imitate the
reservation or deploy as part of source-only landing.
