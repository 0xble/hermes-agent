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
Script jobs can still wait behind another ordinary script, bounded by the existing
script timeout. Frozen person-send wrappers can opt into a separate serial per-profile
`person-send` lane with the exact first line `# hermes: person-send`, but only for
`no_agent` jobs with a one-shot schedule and `deliver=local`. This is an author
scheduling declaration, not send authorization or a sandbox. Do not infer eligibility
from job names, IDs, script names, or message content. Read at most the marker-sized
first line, within the owning profile's scripts root; malformed/missing/escaped
paths retain ordinary script admission. New prepared wrappers opt in; existing
scripts and cron records are never rewritten by the scheduler.

The additional lane keeps one worker, FIFO order and the existing script timeout;
it does not promise hard real-time delivery or shorten provider/readback deadlines.
A prior person-send may still occupy this lane. Validate the persisted job and current
marker under the fire-claim lock before leasing. If edited out of class, hand it to
the ordinary script FIFO (or general pool for agent mode), preserving the original
execution and in-flight owner without starting a lease while queued. This does not
increase ordinary-script or agent concurrency. The older `TERMINAL_CWD` serialization
hazard in upstream #73804 must not return: current script spawning uses subprocess
`cwd` and profile-scoped child environments; agent cwd uses ContextVars.

Preserve the common execution journal, copied profile context, per-job guard,
cancellation and process-exit fences. Departure and shutdown reclaim all auxiliary
lanes. No new dependency, config, job-store schema, provider or transport is added.

### Person-send evidence and upstream design

Independent hypothesis (2026-11-04, before discussion search): the serial no-agent
FIFO introduced in fork [#925](https://github.com/0xble/hermes-agent/pull/925) prevents
agent starvation but explicitly permits script-vs-script head-of-line delay. A
separate narrow serial class is preferable to raising worker counts or introducing
another scheduler. A real tick/fire-claim regression on base
`2fece2a7529e495c2026be0f4b92ed0231678c4a` failed waiting 3 seconds for the fake
person-send marker while an ordinary script held its event. The corrected path
starts the real marker-only subprocess before releasing that event.

Related upstream [#138072](https://github.com/NousResearch/hermes-agent/pull/138072)
is open and unreviewed: it separates agent/no-agent pools, not two script classes,
so it cannot replace this fix. Closed upstream
[#73804](https://github.com/NousResearch/hermes-agent/issues/73804) documents the older
process-global cwd/environment hazard; its automated triage is not maintainer
approval. Current upstream/fork source is already ContextVar/subprocess-cwd based.
No equivalent released/merged person-send admission implementation was found in
open/closed issue, PR or relevant commit searches. This is a fork-policy extension,
not a claim that unmodified upstream contains fork #925's exact regression.

Qualification: `tests/cron/test_person_send_admission.py` and
`tests/cron/test_script_admission.py` cover class refusal, actual script overlap,
ordinary FIFO, edited-job reroutes, execution/dedup retention, serial per-profile
capacity, departure/shutdown, and real concurrent subprocess cwd/profile-home
isolation. No transport is called. Native delivery budgets are unchanged; marked
jobs deliver locally. External provider scheduling declarations do not grant a rate
budget: the prepared-send owner must retain its provider/conversation throttling and
readback checks. Rollback removes only this lane/classification and its reroute
extension, returning marked scripts to the existing serial script lane. Retire this
private extension when upstream preserves the same narrow-class start, claim,
profile and shutdown contract. Source publication only: do not merge, promote or
restart as part of this change.

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

Retain the 120-second grace for ordinary catch-up. Persist each profile's actual
scan-start instant in `ticker_due_scan` after evaluating that scan, separately
from the provider's tick-completion heartbeat. The marker contains epoch, PID,
and process nonce; only this process's previous scan at/before the job's grace
deadline proves continuity. Heartbeats refreshed after another profile stalls
must never replace that evidence. Missing/malformed markers, a different PID or
nonce (including a recycled PID), or a scan after the deadline establish no
continuity. This distinguishes scheduler delay from downtime in the diagnostic,
but neither authorizes an overdue person-facing send.

**Interaction with #1068 (`cron-missed-oneshot-hold`): held wins.** Prompts and
scripts can send to people even with `deliver=local`, so never infer replay
permission from the output lane. Keep every overdue never-started one-shot
payload disabled with `state=missed`, its original schedule and payload intact,
and a durable per-occurrence owner decision notice. Preserve non-local
`failure_deliver`; local/unset failure lanes fall back to origin/home. No scheduler-continuity or
store-outage catch-up path may bypass that hold. An owner must approve a new
time/run-now or remove the job. Consumed/ambiguous dispatches retain the separate
interrupted/partial-effects retirement contract. This supersedes this PR's prior
automatic same-process catch-up policy under the explicit safety steering.

Before holding an unclaimed miss, atomically insert a terminal failed execution
with the scheduled instant and original scan time in its missed reason; never
commit an intermediate claim. Serialize occurrence lookup/insertion in the same
ledger transaction and reuse that occurrence on incident/jobs-save retries.
Protect each just-inserted terminal miss from its transaction's own age/cap
prune with the ledger's existing current-row keep IDs. A same-job attempt
with a newer timestamp, or an equal timestamp and higher ID, may remain the
latest row; both latest and current rows can exceed the best-effort cap.
`tests/cron/test_missed_execution_retention.py` proves durable readback and retry
reuse in both orderings, the two nonfailing controls, and bounded cleanup with
eligible history plus in-flight, delivery and journal protections. The production
100,000-row backstop and 8-day default are unchanged; live incidence is unproven.
Persist the incident too. If either store fails, keep the job for retry.
Commit the stable `oneshot-missed:<job>:<instant>` owner-notice receipt before
persisting the hold, then let normal outbox draining send outside the jobs lock.
Never bypass it with a direct crash-failure send. Spent dispatches retain the
interrupted/partial-effects diagnostic and must not be replayed. Keep claimed
and on-time behavior unchanged. The implementation belongs in
`cron/jobs_oneshot.py`, not the jobs facade.

An explicit re-arm resets the occurrence's dispatch budget and claims but keeps
historical `last_run_at`. Never use that history as evidence that the new arm
started: share the per-occurrence predicate across grace retirement and dispatch
retirement/diagnostics, including recovered missing slots. Slot recovery only
restores the scheduled instant; it delegates eligibility to those same gates.
A consumed current-arm budget
or a started ledger row for its exact scheduled instant proves partial-effects
risk; queued or never-started missed rows and previous occurrences do not.

`tests/cron/test_oneshot_occurrence.py` covers CLI `--at`/`--run-now` re-arms,
held payloads/origin notices, and interrupted/completed occurrence handling.
`tests/cron/test_oneshot_missed.py` covers actual single/multiplex provider
stalls, startup/downtime evidence, on-time grace, and accounting/incident/save
failure retries through real temp-home stores. `test_oneshot_person_hold.py`
proves the hold applies to local agent prompts and declared/non-local sends.
No execution-status migration or live-store change is needed.

Claim presence is not liveness: only `_claim_is_live` may protect a run/fire
claim as in flight, including the started predicate and both retirement gates.
Dead same-host owners and expired leases must settle consumed dispatches with
an interrupted/partial-effects retirement receipt, inside and beyond grace.
The beyond-grace stale-claim wedge also existed on the base and is fixed by the
same liveness rule. Consumed budgets and started ledger rows remain independent
current-arm evidence; never replay those dispatches.

`missed` is terminal for automatic scheduling, not a completed run. Default CLI
list, doctor and dashboard overdue helpers surface the held payload as needing
owner action even disabled. A stale ticker is not blamed for an intentional hold.
Completion retention only prunes `completed`, never `missed`: keep held payloads
until the owner explicitly rearms or removes them. `test_oneshot_claim_recovery.py`,
`tests/hermes_cli/test_cron_missed_state.py` and the web cron-job suite cover these
contracts. Shared isolated one-shot fixtures live in `tests/cron/oneshot_helpers.py`.

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
