# Cron Admission

**Patch identities:** `cron-protected-admission`, `cron-max-catch-up-lateness`.

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
Additional protected agent jobs log an error once per loaded class and use the
general executor best-effort in its unchanged FIFO order. A long run holding
the reservation must not occupy a general worker or overlap itself.

Script-only (`no_agent`) jobs outside that selected reservation use a separate
serial FIFO lane per profile, managed by the same admission pool lifecycle.
They never queue behind detached agent waiters, even while the protected
reservation is busy. Agent jobs, including monitor or pre-run scripts that may
wake an agent, cannot enter this lane. This adds 1 script worker, not agent
capacity: the general cap and the single protected reservation are unchanged.
Script jobs can still wait behind another script, bounded by the existing
script timeout. Preserve the common execution journal, copied profile context,
per-job guard, cancellation and process-exit fences. Departure and shutdown
reclaim both auxiliary lanes.

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
Do not replace it with a higher general cap, a second protected reservation, job-specific
identity matching, or an execution-terminal release shortcut. Retire it only
when the selected upstream baseline preserves the same admission and timing
contract under general-pool saturation.

## Maximum catch-up lateness

Recurring jobs can opt into `max_catch_up_lateness_seconds`, a finite,
nonnegative number of elapsed seconds after their scheduled occurrence. Unset
or null retains the historical unlimited/coalesced catch-up behavior; `0` is
strict zero lateness, and equality remains eligible. This is an expiration
policy, not the recurring backlog grace or the one-shot grace. Manual Run now
is not a delayed scheduled occurrence. One-shot behavior is unchanged.

Enforce expiration before catch-up rewrites the original due slot, on external
provider admission, and again at executor/detached-worker entry. Compare UTC
instants, not local wall clocks. A missed occurrence never launches a script or
agent, consumes no repeat budget, and does not pretend to have executed. Persist
`last_status=skipped`, a structured `last_missed_window` result, the diagnostic
output, and the next future slot. Release only the attempt's own claim and
pending slot. A pre-claim tick snapshot never owns a copied live claim; leave
that owner's stored record untouched. After a missed-window store commit,
notify the external provider outside the store/firing locks so it can arm the
next occurrence. Reject skipped-slot redeliveries only while `next_run_at`
still matches the miss's `advanced_to` slot, not for the job's whole lifetime.

Commit one owner failure notice to the existing durable cron delivery outbox
before advancing the job. Key it by job and canonical scheduled instant so
repeated scans, queue retries, and a restart between outbox and job-store commits
cannot duplicate an attempted send. Owner notices bypass local-only normal or
failure output lanes and use the existing origin/home/incident failure route.
If queue admission fails, do not run or discard the expired occurrence. Group
repeated missed windows under the same job-and-cause incident by normalizing
both elapsed durations and the scheduled instant, while retaining an outbox
notice and the latest diagnostic for each distinct occurrence.

Expose the policy through cron create/update tools and CLI, and always return
it in create/update/list tool responses (including `0` and unset/null). An empty
string clears it through the tool; CLI edit supports an explicit clear flag.
No live job or runtime configuration is part of this source-only patch.

Contract tests: `tests/cron/test_catch_up_lateness.py` and
`tests/tools/test_cronjob_catch_up_lateness.py`; qualification also covers all
`tests/cron/` and existing cron tool tests. Retire this patch when the selected
upstream release preserves both admission/start deadlines and the durable,
local-lane-independent owner notice. Rollback reverts these source hooks and
interfaces; an unset field is already backward-compatible. Coordinate the
owner-alert helper with the separate one-shot-retirement patch: it was not on
`origin/main` at this patch's base, and both use the same delivery outbox and
failure-routing contract rather than introducing a transport.

## Missed One-Shots

Retain the 120-second grace for ordinary catch-up. A persisted ticker heartbeat
contains an epoch and PID. A beat from this process at or before the job's grace
deadline means the delayed scan has not had an opportunity to retire that slot:
allow it once, including jobs that became due during the stall. A different PID,
a missing/malformed beat, or a startup beat after the deadline does not establish
continuity. Those expired one-shots must not fire hours late after downtime.
This continuity rule cannot distinguish host suspension from a stalled ticker
inside the same surviving process.

Before removing an unclaimed miss, persist a terminal failed execution with the
scheduled instant and original scan time in its missed reason, plus an incident.
If either store fails, keep the job for retry. Reuse the failed missed execution
when a later jobs save retries. Only after the removal save succeeds, deliver
through the existing failure route outside the jobs lock, retaining the active
ticker's adapters and loop. Keep claimed and on-time behavior unchanged. The
implementation belongs in `cron/jobs_oneshot.py`, not the jobs facade.

`tests/cron/test_oneshot_missed.py` covers same-process stalls, startup/downtime,
on-time grace, and incident/save failure retries through the real temp-home
stores. No execution-status migration or live-store change is needed.

## Dispatch Diagnostics

Local `cron.scheduler_diagnostics` INFO events name the execution, job, profile
home, actual general/reserved/script admission lane, existing ledger code revision,
stage and monotonic timestamp. The copied dispatch context carries attribution
through the executor hop. Direct calls use the imported immutable release when
available, otherwise the revision remains unknown. Do not resolve a moving
`current` pointer or run Git/ledger queries for each diagnostic event.

`executor_queued` precedes submission and `executor_enter` marks callback entry.
`fire_claim_enter` and `fire_claim_exit` bracket the persisted job claim.
Launch events then bracket scope selection, handoff fencing, payload writing,
secret scope, worker environment/specification, spawn and acknowledgement.
Compare timestamps for the same execution, home and code revision. These
milestones distinguish queue wait from fire-claim or launch preparation, but
missing later milestones alone do not identify a cause or prove worker exit.

Log only allowlisted metadata, never names, prompts, environment values,
payloads, acknowledgements or exception objects. Logging failure must not change
claims, admission, cancellation or process-exit fencing. These events add no
scheduling policy, reservation, configuration, alert or live acceptance credit.

## Verify

`tests/cron/test_reserved_admission.py` exercises actual `started_at` ledger
stamps against a fake clock with all 4 general workers blocked, 2 protected
jobs, FIFO overflow, long runs, completion rearming, per-job exclusion,
profile deadlines, and both terminal-row and waiter-error process-exit fences.
`tests/cron/test_script_admission.py` keeps 4 general detached waiters and the
protected waiter live while a due script writes its real marker within the
same fake-clock tick. It also checks script FIFO, profile isolation and an
independent protected reservation. The base fails the script-start bound.
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
