# Restart continuation policy

Load this unit when changing gateway restart recovery, the resume-pending recovery
note, `GatewayConfig` scalar bridging, or any adapter's `interactive_resume` default.

## Required behavior

- `gateway.restart_resume_policy` accepts `ask` or `continue`. `ask` (the upstream
  default) has the auto-resumed turn run no tools and ask in one line whether to
  carry on with the named pending step. `continue` has it finish the pending work
  without any acknowledgement, resuming from the first step with no recorded
  result. Neither wording presents the restart as news (`RESUME_NOTE_PREFIX`).
- `gateway.platforms.<name>.extra.restart_resume_policy` overrides the global value
  for one platform.
- Non-interactive adapters (`interactive_resume = False`: webhook, API server) always
  continue; no policy can make them ask, because nobody is present to answer.
- An unrecognized value fails at config construction and at YAML startup, never
  silently falls back to the opposite behavior.
- The policy survives the YAML startup loader (`gateway/config_loader.py` bridges
  the key by presence). A preserved YAML value alone is not proof that the
  configured behavior survives an update: the loader omitting the key was the
  archived fork's original regression.
- When a real user message arrives while resume is pending, the note addresses
  that message first regardless of policy and skips stale unfinished work unless
  the message asks for it, as upstream's note always has. Only `continue` with no
  new message automatically resumes the pending task, from its first unrecorded
  step. An explicit continuation request in a new message can resume it under
  either policy.
- Explicit `/stop` retires the recovery marker captured before adapter cancellation.
  A newer marker or replaced session created during the cancellation survives. The
  persisted marker token is additive and older routing entries remain readable.
  This strengthens open upstream [#120758](https://github.com/NousResearch/hermes-agent/pull/120758)
  with a conditional clear instead of an unconditional write after awaits.
- A follow-up dequeued as a turn finishes during shutdown is flushed through `gateway/shutdown_flush.py` before its local reference is cleared, so startup recovery can restore its user message. A slot with neither text nor attachments is not written as an invalid pending payload (a caption-less attachment is kept); errors are logged rather than silently claiming preservation.
- A tool-result tail interrupted before an assistant reply closes with a non-empty
  internal marker, not the legacy `Operation interrupted.` text. Exact marker
  echoes and legacy diagnostics are suppressed at delivery; unrelated prose is
  not. The shared closer filters local diagnostics for early-abort callers too.
  Existing transcript rows are never rewritten, preserving prompt-cache prefixes.
  Delegated-child summaries skip this synthetic row and retain earlier real output.
- All remaining adapter slots, runner pending slots, and FIFO overflow tails are spooled under the session key's owning served profile (including a secondary reached via the primary bot). Startup recovery walks the launch home and every served home in that home's runtime scope; a failed profile replay retains its spool without stopping other profiles.
- The auto-resume restart-loop breaker keeps one gap-bounded chain over counted and
  planned boots, exempting only the first planned boot in that chain. The update/service
  SIGTERM handler persists its consumed signal intent atomically in
  `gateway/restart_loop_planned_exit.json`; boot consumes it once even with no pending
  sessions. Signal/home marker timestamps and the chat `/restart` marker's mtime must
  be within the configured chain gap. Unreadable intent grants no exemption; I/O failure
  never blocks shutdown or startup. Regressions: `tests/gateway/test_restart_loop_guard.py`
  and `tests/hermes_cli/test_gateway_restart_loop.py`.

The `routine-restart-resume-note` patch also covers background process completion
notices and delegation interruption/recovery text in
`tools/process_registry_notifications.py`, `tools/async_delegation.py`, and
`tools/delegation_resume.py`, plus `/goal` lifted-barrier text in
`hermes_cli/goals.py`. Routine lifecycle stops report only the unfinished
outcome, preserve exit codes and partial results, and require reconciliation of
non-idempotent effects before retrying; diagnostic cause metadata remains intact.
Untracked processes report unknown outcomes without guessing a cause.
Explicit user-kill attribution, backend-loss wording, and failed-start wording
remain unchanged. Generic `kill_all` is a routine stop, matching the `/goal` barrier
surface. Single and batch interrupted delegation notices carry the same
reconcile-before-retry hint. CLI cleanup passes a neutral delegation stop reason;
historical `CLI shutdown` reasons are sanitized only when presenting notices.
Cron persisted errors and direct owner notices likewise state the unfinished
outcome, without promising a retry for jobs no longer scheduled. The new
`gateway.shutdown.cron_stopped` key carries English text in every locale until
translated, and the retired cause-based keys were removed.

## Provenance and patches

- Fork patch identities: `restart-continuation`, `crash-left-media-resume`. Local narrow patch; no upstream
  submission. Re-port of the archived fork's HERMES-075 (`dc6610ac64`,
  `fab8126cf2`) onto the `v2026.9.14` baseline, where the call sites had moved
  into `gateway/run_turn_runner.py`.
- Upstream tracking: [#57056](https://github.com/NousResearch/hermes-agent/issues/57056)
  introduced the interactive/non-interactive split this policy generalizes.
- Surfaces: `gateway/config.py` (`restart_resume_policy`, `_normalize_restart_resume_policy`,
  `__post_init__`, `from_dict`, `_SCALAR_DICT_FIELDS`), `gateway/config_loader.py`
  (`_TOPLEVEL_BRIDGE`), `gateway/run.py` (`resolve_restart_resume_policy`,
  `build_resume_recovery_note`, `_prepare_resume_pending_message`),
  `gateway/run_turn_runner.py` (`_resume_restart_policy`, `_prepare_turn_message`).

## Shared Replay Admission

Post-restart intake uses one process-wide `ReplayScheduler` with FIFO order inside
priority lanes. Restored human input enters before empty synthetic resumes.
Recovered follow-ups wait for their own session's resume and use the deferred
lane. `gateway.restart_replay_concurrency` defaults to 2. Resumes and delegation, goal and
notification wakes also wait for the gateway-wide turn cap in
[background turn admission](background-turn-admission.md). Live authorized gateway controls bypass
session restore fences. A stopped synthetic resume must lose its pending preclaim
before dispatch and must never be resurrected by the scheduler.

The current recovery owner remains `gateway/run_restart_recovery.py`. Its existing
adapter admission, actual-turn observer, durable input retirement, profile scope,
and restart-loop breaker remain authoritative. A recovery notice is not a delivery
receipt. Deferred follow-ups wait outside scheduler workers for their own session's
resume. Per-session queue claims remain until older input has been dispatched, so
releasing one session cannot admit another session's newer live input first.
Failed resume or replay admission returns follow-ups in their original source-queue
order without decrementing their outstanding input claims. Shutdown drops use the
same ordering, independent of replay priority or drop-callback order, before the
remaining input is spooled. Releasing a reconnect owner or opening the startup
fence schedules another restore drain. A schedule request made during an active
drain remains pending until a follow-up pass, even when the current pass already
scanned readiness and reports no progress.

The cap governs turn openings, not complete long-running turns. After the existing
startup opening window passes, retain the actual turn as the ordering dependency
but release scheduler admission for unrelated sessions. Keep state.db key resolution
off-loop. On shutdown, queued human input returns to its source queue and is
spooled again after scheduler cancellation, before adapter teardown. Idle workers
exit after draining their batch and later enqueue recreates the pool.

Focused regressions: `test_replay_scheduler.py`, `test_startup_resume_admission.py`,
`test_restart_resume_pending.py`, `test_resume_queued_followup.py`,
`test_restart_recovery_outcomes.py`, `test_restore_drain_wakeups.py`, and
`test_shutdown_followup_restart.py` in `tests/gateway/`. This extends the existing
`seamless-restart` patch unit, not a
plugin or a second delivery store.

## Observable restart recovery

The shared recovery decision lives in `gateway/run_restart_recovery.py`; note
receipts and persisted destinations live in `gateway/run_interrupted_notes.py`.
Startup must retain the pending-session snapshot when the restart-loop breaker
trips, so eligible human turns can still receive a bounded interruption note.
The breaker decision must survive the asynchronous refresh and reconnect scans,
including the pending-follow-up spool path. Do not bypass the breaker when note
visibility is ambiguous: that can recreate the model-driven restart loop.

Each candidate emits one `Restart recovery outcome: session=<key> outcome=<value>`
record per boot. Values are `resumed` after the admitted turn completes,
`noted` for an acknowledged/persisted visible interruption note, or
`skipped:<reason>` for a policy, transport, or admission refusal. The startup
pre-claim is handed to the adapter guard right after `handle_message` returns;
otherwise the spawned task bounces the resume off its own sentinel into the
pending slot and the real turn runs in a successor task. Both the resume and
restored-follow-up observers follow `_session_tasks` successors until the event
reports `_agent_turn_succeeded`; a real failed turn is `skipped:turn-failed` and
gets no interruption note (it reported its own failure). Cover this with
`tests/gateway/test_restart_resume_real_adapter.py`, which drives the real
Telegram adapter, not planted `_session_tasks`. A merely
scheduled task, unaccepted internal event, pending note token, or `sending:`
claim is not proof of successful recovery. A dispatch failure releases the
pre-claim and tries the existing bounded note path; it never replays user work.
A `sending:` note is not retried, and ordinary eligible turn recovery remains
available. If both breaker and ambiguous note block recovery, the outcome is
explicitly `skipped:restart-loop-breaker:ambiguous-note`, not a fabricated
`noted`/`resumed` result. Network unavailability and authorization revocation also
remain real constraints; this unit does not promise unconditional visibility.

`reason=shutdown_follow_up` is a future turn even if the predecessor's resume
marker has been cleared. At read time, including for old spools, it uses the
existing drain-deferred admission path instead of becoming a user row with no
turn. A restored prompt owns recovery ahead of a synthetic empty resume. Its
spool survives an admission refusal, and survives adapter admission too:
`handle_message` returns once the session task exists, before the turn leases
the session or persists anything, so the spool is still the only copy. It is
retired at the turn's durable start, when the agent's turn-start flush writes
the user row carrying the inbound `platform_message_id`
(`_retire_spool_once_input_durable`), or when the turn reports a result. From
that point a crash or restart falls to the turn's own `resume_pending` marker
instead of replaying the whole prompt, because the prompt may already have
caused tool effects. A boot that finds a follow-up spool whose input row is
already durable (crash between the row and retirement) drops it
(`input_already_durable`). A failed real turn is
`skipped:turn-failed` with no recovery notice, since its spool is gone and it
reported its own failure.

Restored follow-ups use the same per-session outcome record. Refusal/no-task
outcomes retain the spool, attempt a bounded platform notice, and log
`skipped:<reason>:noted` or `skipped:<reason>:note-unavailable`; adapter admission
without a real turn is explicitly `skipped:accepted-without-turn:<note-status>`.
Run `tests/gateway/test_shutdown_followup_restart.py` as well as the recovery
corpus. The queue drain now lives beside those decisions in
`gateway/run_restart_recovery.py`.

Run `tests/gateway/test_restart_recovery_outcomes.py` and
`tests/gateway/test_restart_resume_admission_identity.py` in addition to the corpus
below. Planned update classification is separately owned by `fix/breaker-update-planned`
(PR #557); this unit does not duplicate it. Per-session admission identity was
already present at the base. No new dependency, configuration, or persistence
schema is introduced.

## Verification

`scripts/run_tests.sh tests/gateway/test_restart_resume_policy.py
tests/gateway/test_restart_resume_pending.py tests/gateway/test_restart_notification.py
tests/gateway/test_multiplex_pending_recovery.py tests/gateway/test_shutdown_flush.py`.
For routine-stop wording, also run `tests/tools/test_routine_stop_notifications.py`,
`tests/hermes_cli/test_cli_delegation_stop_notice.py`,
`tests/cron/test_shutdown_interrupt.py`,
`tests/gateway/test_cron_interrupt_notification.py`, and
`tests/gateway/test_cron_active_work_drain.py`, plus `tests/agent/test_i18n.py`
for locale key parity.
Also run `tests/gateway/test_stop_resume_marker_generation.py` for busy and idle
stop, unrelated interrupts, repeated marks, and durable routing readback.
The policy file must prove: platform override > global > adapter default,
non-interactive safety, platform-neutral continue guidance, validation at
construction and YAML startup, round-trip through `to_dict`, and that
`TurnRunner._prepare_turn_message` reaches the continue guidance.

After promotion, prove it live: interrupt a Telegram turn with a gateway restart
and confirm the auto-resumed turn continues without asking. Source tests alone do
not prove the installed gateway honors the configured value.

## Retirement and rollback

Retire when a released upstream version lets interactive platforms opt into
continue-on-resume. Roll back by reverting the single fork commit and removing
`gateway.restart_resume_policy` (and per-platform overrides) from configuration;
no schema migration is involved. The optional resume-marker token in routing data
can remain after rollback and is ignored by older readers.

## Crash-left replies with attachments

v2026.9.24 settles a turn whose final reply was persisted before a crash by
ledgering its text, then clearing the active-turn marker. The ledger redelivers
text only, so attachments were lost in two places:

- Startup settled a crash-left reply carrying attachments as plain text, and a
  media-only reply read as unfinished. Any reply the live extractor would send
  attachments for (`MEDIA:` tags, markdown or HTML images, bare local files)
  now stays marked and resumes through the normal path.
- Live delivery released the marker once the text row was ledgered, before the
  attachments were sent. A final with attachments now keeps the marker until
  they are delivered.

`crash-left-media-resume` owns both. `tests/gateway/test_crash_left_reply_media.py`
and `tests/gateway/test_final_marker_after_attachments.py` guard them. Offer
upstream; drop once the ledger carries attachments.
