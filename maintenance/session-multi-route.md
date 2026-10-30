# One Session on Several Routes

Load this unit before changing `SessionAuthority._claim_next`, `_route_binding`, `_row_route`,
`recover_native_sessions`, `_recovery_routes`, `LiveSession.claim_route`, or anything keyed by
`live.route` (resident agent, Stop, classic-turn hold, deferred resets).

## Required Behavior

A session can be bound to more than one routing key, for example a topic moved to a Slack thread
whose Telegram key was bound back to the same session. Each route keeps working.

- **Claim:** a queued native or producer row is checked against its own route's entry, as long as
  that entry still names the same logical owner. A route bound to another session falls back to
  the session's live route, which refuses it as before.
- **Recovery:** several bindings for one owner recover as one session, but only when they are
  distinct routes whose entries all name that owner. Two bindings of one route still refuse with
  `admission_conflict`, and so does a route moved to another session. The live route is the
  latest human route. A single binding is checked exactly as before.
- **Route-keyed state:** while a turn runs, `LiveSession.claim_route` is the route it was admitted
  on. The resident agent, the Stop target, and deferred resets use that route, so they follow the
  turn. The classic-turn hold checks both the row's route and the live route. Stop also ends the
  session's background delegations on both routes.
- **Replies:** a turn's reply goes back to the platform its row came from. Producers (`/loop`
  ticks, delegation completions pinned by `origin_session`, process notices, cron targets) keep
  their own routes.

## Source

Fork patch identity: `session-multi-route`.

After the 2026-10-30 Slack cutover, Brian wanted Telegram to keep working in moved topics while
systems post to Slack. A rehearsal on a clone of live state, with release 6a664d31 and each moved
Telegram key bound back to its owner, showed the problem:

- The first message from the second platform paused the session with `admission_conflict`.
  `LiveSession` had a single route, and `_claim_next` checked every row against it.
- That stopped every Slack producer queued behind the message.
- Restart recovery refused any session that had 2 bindings.

With this change, the same rehearsal ran both orderings (Slack, Telegram, Slack and Telegram,
Slack, Telegram) and recovered rows queued on both routes after a restart, with no pause. Replies
went to their own platforms, and a Slack-pinned delegation completion was admitted on Slack.

## Verification And Rollback

Guard: `scripts/run_tests.sh tests/gateway/test_session_multi_route.py
tests/gateway/test_native_operator_provenance.py tests/gateway/test_synthetic_native_admission.py`.

To roll back, revert this identity. Before reverting, unbind any second route
(`rebind_telegram.py reverse`). Otherwise those sessions pause again at the first message from
their second platform.
