# Cron Execution History

Patch identity: `cron-execution-history`.

## Contract

The profile-local execution ledger must support a weekly health audit, failure
streaks, and unknown-attempt coverage without normal fleet volume shortening
history to hours. By default, retain every terminal status for 8 elapsed days after finish,
falling back to claim time for legacy rows without a usable finish timestamp.
The 100,000-terminal-row ceiling is a runaway-write backstop and may shorten
that window only when reached. Claimed and running attempts are excluded.
This is a row ceiling, not a strict byte quota: output sizes can change.

`cron.execution_retention_days` sets the elapsed-day window in `config.yaml`
(default `8`, `0` disables age expiration while retaining the row backstop).
Read it in the owning profile at each terminal transition. The virtual-clock
DST soak configures a window longer than its full simulated span: it proves
every fire and delivery across that span, while dedicated retention tests prove
expiration. Its final all-runs ledger assertions remain unchanged.

Both policies preserve the attempts being settled by the current transition
and each job's latest attempt, using the same parsed claim
instant and tie-breakers as `hermes cron list`. They also preserve attempts
with queued/pending/delivering or provisional delivery state, and any attempt
with an admission journal still present. A terminal detached worker may still
owe canonical settlement, so journal removal by the recovery owner releases
that protection. These current-state and recovery obligations take precedence
over the row ceiling. Cleanup converges to the ceiling only when enough rows
are eligible. Incidents retain their own error and output-file evidence in
`cron_incidents`, with no execution-ID reference requiring a ledger-row pin.

Keep cleanup in the shared terminal-transition path. Each call deletes at most
500 rows across age expiration and ceiling trimming combined, oldest first.
An existing backlog converges over later terminal writes instead of requiring
one large write transaction. The partial numeric-time index serves expiration,
ordering, and bounded counting. A numeric per-job claim index serves the latest
attempt exclusion without repeatedly sorting each job's history. Eligibility
also checks delivery state and journal presence. Compute the cutoff in UTC before subtracting the duration,
and compare parsed instants so offset changes and DST do not move the window.
The index is additive, preserving named-column reads/writes by older releases.

## Sizing Evidence

A 2026-10-29 read-only SQLite backup contained 1,000 terminal rows spanning
6.0407 hours, approximately 3,969 terminal rows/day. The 1,017 total execution
rows occupied 4,911,104 bytes of execution-table/index pages after adding the
retention index, approximately 4,829 bytes/row. The measurement excludes the
separate incident table and freelist, rather than treating the entire 12,423,168
byte file as execution payload.

At that observed rate, 8 days projects to 31,753 rows and 146.2 MiB of execution
pages. The 100,000-row backstop leaves 3.15 times that volume, approximately
460.5 MiB at the sampled density. These are workload projections, not size
promises. A scratch-only rehearsal populated from those real rows trimmed a
101,000-row terminal backlog in 2 batches of 500. Transactions including commit
took 93.3 and 82.9 ms, with a 4.8 ms no-op pass at the ceiling. Expiring a large
old backlog deleted 500 rows in 61.5 ms. Index migration preserved all rows and
took 4.0 ms on the original backup. No live ledger was modified.

## Upstream Disposition

Related upstream [issue #93616](https://github.com/NousResearch/hermes-agent/issues/93616)
is an issue, not a PR. Its associated open
[PR #93858](https://github.com/NousResearch/hermes-agent/pull/93858) makes the
1,000-row cap configurable but does not establish a time window. Related open
[PR #121899](https://github.com/NousResearch/hermes-agent/pull/121899) proposes an
8-day window, but its diff uses unbounded deletes and lexical timestamp
comparison, with a separate unbounded-by-count unknown population. Neither
satisfies this contract. These were inspected as prior art, not adopted patches.
The fork implementation deliberately uses one time policy and one ceiling for
all terminal states, with a profile-local age-window setting and no new scheduler owner.
Upstream state was checked on 2026-10-29. No upstream write is part of this unit.

## Verification and Retirement

`tests/cron/test_execution_retention.py` owns the time-window and bounded-cleanup
invariants, including mixed offsets, DST, the cutoff boundary, missing finish
times, combined policy budgets, configurable windows, and preservation of
in-flight work, current job status, pending delivery, and canonical recovery. Run it
alongside `test_execution_ledger.py` and
`test_restart_safe_worker_death_evidence.py` to cover ordinary finishes,
restart recovery, and dead-worker terminalization.

Retire this divergence only when the selected upstream release passes the same
window, ceiling, timestamp, and bounded-cleanup contract. Roll back the pruning
policy, index creation, and regressions together. The extra index may remain
on disk harmlessly. Rolling back to count-only pruning immediately weakens
future audit coverage and cannot restore already-deleted history. Source
qualification alone does not install or activate the change.
