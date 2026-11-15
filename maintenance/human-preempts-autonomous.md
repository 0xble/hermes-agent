# Typed messages preempt autonomous turns

Load this unit when changing busy-session admission, `busy_input_mode`, goal
continuations, boot auto-resume, completion follow-ups, heartbeats, or relay
classification.

## Required behavior

- With `busy_input_mode: queue`, a typed message still waits behind a turn the
  user opened. That is why the mode exists: follow-ups must not cut into the
  user's own work.
- A turn nobody typed is different. Goal continuations, boot auto-resume,
  completion and background-process follow-ups, heartbeats, `/loop` wakeups and
  agent relays (`[relay from=... receipt=...]` edge header) run in the same
  session and can last an hour. A typed message arriving during one of them
  redirects it: an in-flight model request is re-issued with the message, a
  foreground terminal command yields to the background registry, and nothing is
  killed. When redirect declines (between a model response and its tool batch)
  the message is steered instead, so it reaches the model at the next tool
  boundary. Voice is transcribed and steered. Photos and documents still queue.
- The running turn answers incorporated typed input. Its reply expectation moves
  only when a steer is inserted into model history. Input buffered after the last
  boundary becomes a human successor without changing the predecessor's silence
  contract. A prior incorporated human input still requires a predecessor reply.
  The goal's post-turn continuation resumes the autonomous work.
- When a session authority owns the route, the typed message is not steered. It stays a
  queued FIFO row, because restart recovery replays a queued row but marks a steer handed
  to a lost claim interrupted (`_RECOVER_STEERS_SQL`), which would lose the message. Its busy
  preempt (`_hm_busy_preempt`) sends the queued acknowledgement at once instead, including
  while the autonomous turn is still starting, so the person never waits with no reply.
- Once the user has steered or redirected a turn (`_turn_user_intervened`), it
  is theirs and keeps queue semantics.
- Relays and other agent-origin messages never preempt.

## Source surfaces

`gateway/run_busy.py`: `_human_preempts_autonomous_turn`, the `preempt` branch
in `_handle_active_session_busy_message` and `_resolve_busy_steer_or_redirect`.
`gateway/run_inbound.py`: the queued acknowledgement in `_hm_busy_preempt`, the authority-owned busy path.

## Verification

`scripts/run_tests.sh tests/gateway/test_busy_human_preempts_autonomous.py tests/gateway/test_late_steer_reply_obligation.py tests/gateway/test_authority_busy_preempt_parity.py tests/gateway/test_authority_automation.py`

The late-steer regression covers declined redirects, buffered successor delivery
exactly once, incorporated steering through busy/priority/slash/tool-redirect
routes, and a prior human obligation surviving a later leftover.

## Provenance

- Fork patch identity: `human-preempts-autonomous`.

- Evidence (2026-10-11): typed messages waited 57-85 minutes behind boot-resume,
  relay and cron turns in the same topic, including a request to pause the work
  that was running.
- Evidence (2026-11-15): under the session authority, "check now" was admitted
  behind a 68-call goal turn and waited 12 minutes with no reply. Steering it in
  instead was reverted: a restart would have lost it.
- Upstream has no equivalent; upstream defaults to `interrupt`, which applies to
  every turn. Retire this when upstream distinguishes autonomous turns in busy
  admission.
- Rollback: revert the commit; behavior returns to plain queue semantics.

Contract tests: `tests/gateway/test_busy_human_preempts_autonomous.py`, `tests/gateway/test_late_steer_reply_obligation.py`, `tests/gateway/test_authority_busy_preempt_parity.py`.
