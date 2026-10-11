# Cron in-flight cancellation

**Patch identity:** `cron-inflight-cancellation`.

Removing or pausing a schedule used to leave its in-process agent alive until the
fire-claim heartbeat noticed the change. The caller could see scheduled/never-run
and retry an already-started side effect. Preserve immediate cancellation through
the existing scheduler ledger and agent hard-interrupt path, truthful mutation
receipts, and a visible stable running timestamp.

Source: `cron/scheduler_cancel.py`, `cron/scheduler_control.py`, `cron/jobs.py`,
`cron/scheduler.py`, `tools/cronjob_tools.py`, `tools/cronjob_job_args.py`.

Proof: `tests/cron/test_inflight_cancellation.py` holds the heartbeat at one hour,
removes/pauses an active run through registry dispatch, verifies synchronous hard
interruption before another side effect, and checks running, remote-claim and idle
receipts. Preserve existing self-removal delivery semantics (#111039): a run
removing its own schedule is not an external operator stop. Detached workers are
not falsely reported interrupted; remote pause must invalidate heartbeat renewal.

An explicit `update_job(..., {"enabled": False})` has the same stop intent as pause:
interrupt a local active run immediately, revoke detached/remote heartbeat renewal,
and preserve acknowledged delivery. Renewal rejects all non-runnable schedules;
owner-fenced terminal bookkeeping still releases a disabled run's claim. Auto-pause
and self-pause retain their existing delivery exceptions.

Dashboard/transport drain is separate: its explicit cancellation remains fail-closed
(#105861), even during delivery. Only schedule pause/remove/disable receives the
acknowledged-send cancellation exemption; sampled heartbeat misses retain their
existing completed-delivery exemption. Keep the transport-cancel regression intact.

Queued controls may be rearmed by pause/resume before the run starts. Once a finite
one-shot starts, cancellation stamps its true terminal outcome and keeps the dispatch
already consumed by `claim_dispatch`; it does not refund `repeat.completed` or imply
ordinary resume can replay that occurrence. Explicit one-shot re-arm is required.
Recurring cancellations do not consume a completed-run repeat or failure budget.

Listing and tool formatting inspect process-local controls only if `cron.scheduler`
is already loaded. Otherwise the durable live claim supplies running visibility;
scheduler lookup failures must not break these read-only responses.

Verification must include `tests/cron/test_fire_claim_lost_after_delivery.py` and
`tests/cron/test_inflight_cancellation.py`; a passing exact-SHA gate alone does not
prove these paths ran. The bounded integration check is
`bash ./scripts/run_tests.sh tests/cron tests/tools/test_cronjob_tools.py -j1 -q`.
The v0.21.6 integration retains registration-owner fencing and late-future attachment,
per-profile restart-wait accounting, unstarted execution settlement, and retry
`ladder_rung` accounting. Cancellation controls register and release inside the same
owner-fenced critical sections; cancellation does not spend another retry occurrence.
`tests/cron/test_cancellation_registration_fencing.py` checks these combined boundaries.

Terminal bookkeeping must first confirm ownership: stale and replacement owners
never call `mark_job_run`, while a paused own claim is still released through its
owner-fenced terminal write. `tests/cron/test_autopause_delivery.py` covers this
and scheduler auto-pause of malformed legacy jobs. Auto-pause may deliver the
blocked-run reason from that exact run-local pause; an operator pause still
interrupts it, and another owner or heartbeat context receives no exception.

Upstream candidates: NousResearch/hermes-agent issue #133057 and PR #133169
(`hermes cron cancel`); inspect their semantics before adoption. Retire this patch
only when upstream also covers remove/pause, tool receipts, and running visibility
with the same regressions green. Source landing is not live gateway deployment.
