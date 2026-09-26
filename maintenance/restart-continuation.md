# Restart continuation policy

Load this unit when changing gateway restart recovery, the resume-pending recovery
note, `GatewayConfig` scalar bridging, or any adapter's `interactive_resume` default.

## Required behavior

- `gateway.restart_resume_policy` accepts `ask` or `continue`. `ask` (the upstream
  default) has the auto-resumed turn report the restore and wait for the user.
  `continue` has it finish the interrupted work without a "session restored"
  acknowledgement, resuming from the first step with no recorded result.
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
  that message first regardless of policy.
- A follow-up dequeued as a turn finishes during shutdown is flushed through `gateway/shutdown_flush.py` before its local reference is cleared, so startup recovery can restore its user message. Empty text is not written as an invalid pending payload; errors are logged rather than silently claiming preservation.

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
tests/gateway/test_restart_resume_pending.py tests/gateway/test_restart_notification.py`.
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
no schema or persistent-data change is involved.

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
