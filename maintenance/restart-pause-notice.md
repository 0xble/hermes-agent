# Restart-paused chats are told they are paused

Fork patch identity: `restart-pause-notice`.

## Ownership

- Owner: gateway native admission / unknown-execution recovery.
- Scope: `gateway/session_pause_announce.py`, its call from `SessionAuthority._pause`
  and the `unknown_execution` branch of `SessionAuthority.recover_native_sessions`, plus the
  `fenced` collector of `hermes_state_runtime.recover_session_inputs` that `_claim_epoch` passes.
- Incident: after a forced gateway restart, startup recovery paused 4 Telegram topics on
  `unknown_execution`. The person's messages already queued behind each lost turn never ran,
  and the chats stayed silent for about 35 minutes, because `pause_notice` only reaches the
  user on the reply path of a message whose waiter the pause releases or of a new message.
  Recovery has neither: the previous owner admitted the queued rows.

## Contract

An `unknown_execution` pause schedules one unprompted notice per pause episode, the same
generic `pause_notice` text, through the chat's normal delivery adapter, marked interim.
`LiveSession.pause_notified` is shared with the reply path, so whichever reaches it first sends
and a later message onto the same pause is saved silently. A failed send clears the flag so
the next message still carries the notice. No notice is sent when no person's input waits
(only unattended rows per `session_unattended_unknown.is_unattended`), to a known bot, to an
app-only LOCAL session, without a deliverable adapter, while draining, or from an owner whose
epoch is no longer the store's. Other pause reasons are unchanged.

The episode outlives the process, but `pause_notified` does not, so startup recovery announces
only the sessions whose lost turn this boot fenced (`recover_session_inputs(..., fenced=)`,
kept as `SessionAuthority.fenced_at_boot`). A pause an earlier boot already left `unknown` is
not told to the chat again by every later restart; the next message's reply path still carries
it. Without this, each gateway restart re-sent the notice to every chat still paused.

## Upstream relationship

Upstream has the same reply-path-only notice (GW-10, #141357). Remove this patch once
upstream announces recovery pauses to the chat and the contract test passes on it.

Contract tests: `tests/gateway/test_restart_pause_announce.py`,
`tests/gateway/test_session_authority.py::test_native_queue_survives_owner_kill_without_replaying_unknown`,
`tests/gateway/test_native_ingress_media.py::test_accepted_media_survives_mutation_cleanup_and_owner_kill`.
