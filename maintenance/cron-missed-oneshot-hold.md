# Missed one-shots and delayed-send freshness

Fork patch identity: `cron-missed-oneshot-hold`.

## Contract

Never-started one-shots that miss the 120-second grace remain stored, disabled
and marked `missed`, with their exact prompt/payload and timing evidence. Admit
one durable decision notice per occurrence through the existing failure outbox,
preserving explicit non-local `failure_deliver`; unset/local lanes fall back to
the creating origin then configured home. Admit that notice before committing
the hold. The notice offers explicit `cron resume <id> --run-now` or
`cron remove <id>`. Store-outage coverage explains the miss but never authorizes
an hours-late send. Consumed/ambiguous dispatches retain their existing distinct
retirement and partial-effects notice. `repeat.completed` counts consumption of
this occurrence; a preserved `last_run_at` from before explicit rearm is history,
not evidence that the new occurrence started.

Reject malformed declared SHA-256, missing/mismatched payloads and malformed
structured freshness contracts before create, update or reactivation persists.
In prompt text, a literal `Expected SHA-256:` value explicitly declares a digest;
an unqualified `sha256:`/`sha256=` value declares one only when it is hexadecimal.
Markdown quoting is accepted. Shell substitutions and angle/brace placeholders
are not literal digests, and unrelated checksum prose is not a send contract.
Structured `payload_sha256` and freshness digest bindings remain strict.
An unrelated disable/pause must still work for malformed historical records.
The model-tool handler forwards these fields; schema exposure alone is not enough.

Use the common `run_one_job` boundary, including external/detached/manual fires,
to recheck the deadline and declared freshness without the jobs lock. Recheck
both time and current occurrence/approval after live reads. A superseded snapshot
stops without disabling its replacement. A manual rearm remains subject to fresh
thread/activity evidence; it is not permission to ignore new replies. Reader
exceptions fail closed through the same held-payload/decision-notice path and
settle the execution ledger before dispatch, rather than leaving a running row.

The communications edge owns provider semantics: final check-then-transport
wrapper, approved watermark, exact argv/body digests, live account-bound Slack
thread/huddle reads and recipient-attendee meetings on scoped sender calendars.
Do not duplicate account APIs or an exhaustive people-activity service in core.
Unsupported providers and unavailable scoped signals must be reported explicitly,
not represented as verified quiet.

## Ticker recovery

Admission-journal recovery can wait 20 seconds per remote receipt. It must not
run serially before the due scan. Coalesce into one bounded off-tick recovery
worker, one pending pass per home, with captured home context. Keep journals as
the durable retry queue and observe/log errors. Headless recovery still cannot
connect or spawn a gateway. Do not occupy a job-dispatch worker.

### Integration blocker

The merged #968 expiry owner can retire a dead-owner, started one-shot while
its off-tick canonical receipt observation is pending. The isolated barrier
regression `test_current_arm_receipt_survives_concurrent_expiry_scan` preserves
this failure: the job disappears before `mark_job_run` can land. The receipt
may settle its execution row, but that is not coherent job/canonical settlement.
Do not treat a green subset or the prior branch's hosted success as qualification.
Resolve receipt-versus-retirement ownership with the parent before activation;
no automatic historical replay is permitted.

## Source and proof

- `cron/jobs.py`, `cron/oneshot_retirement.py`, `cron/scheduled_send.py`,
  `cron/scheduler.py`, `cron/scheduler_authority.py`, `cron/scheduler_tick.py`.
- `tools/cronjob_tools.py` public input forwarding and validation.
- Tests: `test_oneshot_grace_due_scan`, `test_oneshot_retirement_alert`,
  `test_scheduled_send_hold`, `test_original_scheduled_send_reproductions`,
  `test_recovery_tick_nonblocking`, and adjacent recovery/store tests.
- Run via `scripts/run_tests.sh` with scratch `HERMES_HOME` and explicit scratch
  `HERMES_RUNTIME_DIR`; never exercise imports against a live state database.
- The original 297-second deletion and 61-character checksum creation are pinned
  as expected failures against immutable baseline source; candidate equivalents
  must pass. Source archives may skip only the historical Git-source reproduction.

## Upstream disposition and stack fit

Merged #968 (`a62993bb50d8c00320e228cf5680b38380c99c57`) owns due-scan
expiry, `occurrence_started`/`occurrence_finished`, claim liveness, atomic
`record_missed_execution` accounting and its best-effort cap protection, and
`held_outcome`/failure-lane routing. Keep those implementations rather than a
second facade expiry/hold path. Admission failures remain immutable evidence;
an overdue recovery adds one distinct, terminal never-started miss per
occurrence, not another dispatch attempt. Protected current/newest ledger rows
may exceed the retention cap; the shared 500-row cleanup budget is unchanged.

The remaining fork delta owns create/update/reactivation validation, the
shared worker-entry deadline/freshness fence, recovery-notice wording, and
nonblocking canonical receipt recovery. These are not superseded by #968.

Keep this fork patch until upstream preserves held payloads, durable decisions,
full shared-runner deadline/freshness boundaries and nonblocking recovery. A fix
that simply fires missed one-shots after downtime does not satisfy this contract.
Reuses the existing job store, claim fences, failure outbox, runner and native
provider CLIs. No new scheduler, service, transport or gateway lifecycle owner.
