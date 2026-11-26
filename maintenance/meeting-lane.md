# Live meeting topic lane

## Required behavior

An explicit `meetings` auto-skill binding on a Telegram topic selects steer for
ordinary busy text, independently of the routed profile's queue/interrupt setting.
A single event policy is used before adapter busy handling, the running-session
priority path, and durable FIFO admission. Successful steering consumes the event
once; failed steering still queues it. Removing the binding restores configured
busy behavior. Titles never select policy.

Authorization, commands, internal events, passive FYIs, drain, compression and
non-text handling keep their existing guards. Other topics and platforms keep
their configured busy mode.

The same binding defaults the existing durable session reasoning override to
`medium` when absent, both before a cold turn and before a busy steer. Existing
session picks (including reasoning off) are preserved. The live agent receives
the default on its next request through the existing inference-control setter.
No `model.attended` or other global/profile configuration changes. Removing the
binding does not delete a persisted session pick. While the binding remains,
`/reasoning reset` is temporary: the next ordinary meeting message restores the
lane's medium default. After unbinding, `/reasoning reset` clears the pick and the
next message uses the configured effort.

## Provenance and verification

Fork patch identity: `gateway-meeting-lane` (`Fork-Patch: gateway-meeting-lane`).
Approved live-meeting lane design, sections 2–3. Related units:
`human-preempts-autonomous.md`, `live-inference-controls.md`,
`session-reasoning-persistence.md`, and `attended-model-switching.md` retain their
independent contracts. No upstream PR #745 code is included.

Focused tests: `tests/gateway/test_multiplex_busy_input_mode.py`,
`tests/gateway/test_busy_command.py`, and `tests/gateway/test_busy_session_ack.py`.
Run these with an isolated `HERMES_HOME`, plus touched-file ruff and
`scripts/check --only health`.

## Retirement and rollback

Retire when an adopted upstream release offers equivalent skill-bound, event-local
busy steering at every admission boundary and a non-destructive session reasoning
default. Revert the commit carrying this fork patch trailer. A previously persisted
medium override remains an ordinary session pick and can be cleared with the
existing `/reasoning reset`; no state migration is necessary.
