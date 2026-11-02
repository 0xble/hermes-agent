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
- The turn then answers the typed message (reply anchor and reply expectation
  move to it). The goal's post-turn continuation resumes the autonomous work.
- Once the user has steered or redirected a turn (`_turn_user_intervened`), it
  is theirs and keeps queue semantics.
- Relays and other agent-origin messages never preempt.

## Source surfaces

`gateway/run_busy.py`: `_human_preempts_autonomous_turn`, the `preempt` branch
in `_handle_active_session_busy_message` and `_resolve_busy_steer_or_redirect`.

## Verification

Contract tests: `tests/gateway/test_busy_human_preempts_autonomous.py`

`scripts/run_tests.sh tests/gateway/test_busy_human_preempts_autonomous.py`

## Provenance

- Fork patch identity: `human-preempts-autonomous`.

- Evidence (2026-10-11): typed messages waited 57-85 minutes behind boot-resume,
  relay and cron turns in the same topic, including a request to pause the work
  that was running.
- Upstream has no equivalent; upstream defaults to `interrupt`, which applies to
  every turn. Retire this when upstream distinguishes autonomous turns in busy
  admission.
- Rollback: revert the commit; behavior returns to plain queue semantics.
