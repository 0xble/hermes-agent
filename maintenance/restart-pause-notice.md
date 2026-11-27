# Restart-paused chats are told they are paused

Fork patch identity: `restart-pause-notice`.

## Ownership

- Owner: gateway native admission / unknown-execution recovery.
- Scope: `gateway/session_pause_announce.py`, its call from `SessionAuthority._pause`,
  the `unknown_execution` branch of `SessionAuthority.recover_native_sessions`, and the
  reply path's `gateway.session_ingress.pause_notice` and `settle_pause_notice`, which the
  adapter's `_process_message_background` (`gateway/platforms/base.py`) calls with the outcome
  of the reply that carried the notice.
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
the next message still carries the notice. A message whose reply path runs while the unprompted
send is still in flight waits for that send's outcome (`await_announcement_send`), so a send that
then fails leaves this message's reply to carry the notice, never silence on both paths. No notice is sent when no person's input waits
(only unattended rows per `session_unattended_unknown.is_unattended`), to a known bot, to an
app-only LOCAL session, without a deliverable adapter, while draining, or from an owner whose
epoch is no longer the store's. Other pause reasons are unchanged.

The episode is the session's earliest `unknown` admission. It outlives the process, but
`pause_notified` does not, so a successfully delivered notice is recorded durably: a
`state_meta` row `pause_notice:<admission_id>`, written off the loop after a successful
unprompted send, or once the adapter reports that a reply carrying the notice was delivered. A
reply whose send failed leaves the episode untold: `pause_notified` is cleared, so the next
message carries it, and no marker is written, so the next boot announces it. Every boot schedules the
announcement for each session paused on `unknown_execution`. A session whose episode already
has a marker is not told again, and its `pause_notified` is set so a later message does not
repeat it either. A boot whose send failed leaves no marker, so the next boot announces it.
Without the marker each restart re-sent the notice to every chat still paused. Before it, a
"fenced by this boot" rule (November 2026) avoided that repeat, but a failed first send was
then never retried, leaving the person's saved message silent across later restarts.
Each marker write drops markers whose admission is no longer `unknown`, so at most one per
paused session remains.

## Upstream relationship

Upstream has the same reply-path-only notice (GW-10, #141357). Remove this patch once
upstream announces recovery pauses to the chat and the contract test passes on it.

Contract tests: `tests/gateway/test_restart_pause_announce.py`, `tests/gateway/test_native_pause_notice.py`,
  `tests/gateway/test_session_authority.py::test_native_queue_survives_owner_kill_without_replaying_unknown`,
  `tests/gateway/test_native_ingress_media.py::test_accepted_media_survives_mutation_cleanup_and_owner_kill`.
