# Restart-paused chats are told they are paused

Fork patch identity: `restart-pause-notice`.

## Ownership

- Owner: gateway native admission / unknown-execution recovery.
- Scope: `gateway/session_pause_announce.py`, its call from `SessionAuthority._pause`
  and the `unknown_execution` branch of `SessionAuthority.recover_native_sessions`.
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

## Upstream relationship

Upstream has the same reply-path-only notice (GW-10, #141357). Remove this patch once
upstream announces recovery pauses to the chat and the contract test passes on it.

Contract tests: `tests/gateway/test_restart_pause_announce.py`.
