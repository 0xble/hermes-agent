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

## Freshness checker process and path ownership

Freshness checkers reuse the native fire secret scope and profile terminal policy
before building their child environment with `build_subprocess_env` and
`strip_launch_profile=True`. This applies before detached-worker handoff too;
never inherit a multiplex launch profile's raw environment. Restore both scopes
when building the environment finishes.

`send_freshness.script` uses the ordinary `HERMES_HOME/scripts` confinement owner.
The only compatibility exception is the installed `communications` skill's exact
`scripts/send_freshness.py`, resolved through `get_skill_search_roots` and
`locate_skill` with project discovery excluded. This preserves the deployed
skill-directory symlink and configured installed roots without allowing arbitrary
external Python, other resources, project-local overrides, traversal, or resource
symlink escape. Apply `check_gateway_lifecycle` to both kinds at declaration and
immediately before execution. Missing, changed or unscannable checkers hold with
`checked=false`, never a fresh verdict. This is the existing heuristic script
contract, not an OS sandbox or a claim of atomic protection against a concurrent
filesystem writer.

The P1 regression suite exercises a real synthetic A→B→A child with fake sentinel
credentials, declaration/create/update rejection, mutation at fire, unavailable
readers and dispatch suppression. Existing checker fixtures live under the same
scripts owner; their delivery/payload assertions remain unchanged.

Guidance reconciliation: pinned upstream `93ce7e34573aa942b6c44cfc17983a93dcd9d637`
AGENTS/contribution/cron guidance favors profile-island tests and existing owners.
Source prior art is `scheduler_script`, `_fire_secret_scope`, `terminal_scope`,
`skill_utils` and `skills_tool`, not another registry or scheduler. Open upstream
#94655 reinforces lifecycle scanning on update; it is related guidance, not a
replacement for this fork-only freshness boundary. Credential/path hardening is
within SECURITY.md's in-process heuristic posture, not a new isolation boundary.
Detailed local RED/GREEN receipts and source/prior-art notes live in the source
candidate handoff; hosted and loaded-runtime acceptance remain separate.

Out-of-scope review follow-ups remain recorded, not repaired here: recovery
jobs-lock across delivery (`scheduler_authority.py`), whole-claim comparison
(`jobs.py`), duplicate freshness check (`scheduler.py`) and recovery logging
(`scheduler_authority.py`). Preserve every existing failure-lane, per-leaf,
authenticated-reader, loaded-canary and live-guard activation condition.

## Ticker recovery

Admission-journal recovery can wait 20 seconds per remote receipt. It must not
run serially before the due scan. Coalesce into one bounded off-tick recovery
worker, one pending pass per home, with captured home context. Keep journals as
the durable retry queue and observe/log errors. Headless recovery still cannot
connect or spawn a gateway. Do not occupy a job-dispatch worker.

### Current-occurrence canonical recovery ownership

Expiry, due-scan dispatch limits and direct dispatch claims share the same local
recovery-obligation check. Only the current scheduled instant's execution and
its deterministic canonical admission link can suppress both redispatch and
destructive retirement. `unknown` remains recoverable. Capture import alone
(`canonical_settled`) does not prove the scheduler tail is finished; completed
or failed canonical bookkeeping does. No-journal dead partial-effects retirement
keeps #968's existing policy.
Queued/admission-only evidence is never promoted to actual payload-start proof.
Unreadable or corrupt current links stay held with a deduplicated incident.

Canonical observation stays off tick and outside the due-scan jobs lock. A late
old-arm receipt settles its original execution and preserves its document, but
never sends or changes the replacement arm's budget, status, claims or schedule.
The common completion reducer also checks this occurrence fence; output/send
fences recheck it under the jobs lock against concurrent operator rearm. No new
registry, lifecycle flag, automatic replay or historical journal scan is added.

The barrier regression `test_current_arm_receipt_survives_concurrent_expiry_scan`
and `test_oneshot_recovery_ownership` qualify the target through terminal job,
execution and canonical-link settlement. This is source/disposable-state proof,
not loaded-runtime or person-transport proof. Hosted qualification and activation
remain separate parent-owned gates.

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
