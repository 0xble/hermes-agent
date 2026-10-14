# Internal recovered follow-up retirement

## Fork patch identity

`internal-followup-retirement`.

## Behavior

Internal shutdown follow-ups have no platform message id. Recovery uses the exact
user content and spool timestamp to detect a durable turn-start row, so a crash
between transcript persistence and spool unlink cannot replay the prompt. When a
recovered event is merged into a busy adapter's pending slot, the background task
records the event it actually consumes and restart recovery carries that task's
terminal result back to the parked recovery event. The recovery outcome is then
`resumed` or `skipped:turn-failed`, rather than `accepted-without-turn`.

## Source surfaces

`gateway/run_pending_recovery.py`, `gateway/run_restart_recovery.py`,
`gateway/platforms/base.py`, `gateway/session_transcript.py`,
`hermes_state_messages.py`, `gateway/shutdown_flush.py`, and
`tests/gateway/test_shutdown_followup_restart.py`.

## Verification

Focused restart-recovery tests cover id-less durability, spool retirement at
turn start, and pending-slot successor result propagation.

## Rollback

Revert the commit carrying `Fork-Patch: internal-followup-retirement`.
