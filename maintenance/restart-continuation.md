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

The `routine-restart-resume-note` patch also covers background process completion
notices and delegation interruption/recovery text in
`tools/process_registry_notifications.py`, `tools/async_delegation.py`, and
`tools/delegation_resume.py`, plus `/goal` lifted-barrier text in
`hermes_cli/goals.py`. Routine lifecycle stops report only the unfinished
outcome, preserve exit codes and partial results, and require reconciliation of
non-idempotent effects before retrying; diagnostic cause metadata remains intact.
Untracked processes report unknown outcomes without guessing a cause.
Explicit user-kill attribution, backend-loss wording, and failed-start wording
remain unchanged.

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

## Verification

`scripts/run_tests.sh tests/gateway/test_restart_resume_policy.py
tests/gateway/test_restart_resume_pending.py tests/gateway/test_restart_notification.py
tests/gateway/test_multiplex_pending_recovery.py tests/gateway/test_shutdown_flush.py`.
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
