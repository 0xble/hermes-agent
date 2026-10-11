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
