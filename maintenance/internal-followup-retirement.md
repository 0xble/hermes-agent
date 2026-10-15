# Internal recovered follow-up retirement

## Fork patch identity

`internal-followup-retirement`.

## Behavior

Internal shutdown follow-ups have no platform message id. New spools carry a
synthetic recovery id in a recovery-only field; it is persisted as the user row's
dedupe value and never exposed as transport identity. Recovery uses that id to
detect a durable turn-start row, so a crash between transcript persistence and
spool unlink cannot replay the prompt. Legacy spools without a recovery id use
the exact user content and spool timestamp as a compatibility fallback. When a
recovered event is merged into a busy adapter's pending slot, the background task
records the event it actually consumes and restart recovery carries that task's
terminal result back to the parked recovery event. The recovery outcome is then
`resumed` or `skipped:turn-failed`, rather than `accepted-without-turn`.

A draining turn's leftover `/steer` or interrupt text is only a string. In a DM
or per-user session the shutdown follow-up spool binds it to the turn's source
(author, chat) and a recovery id before writing, so restart recovery can
attribute and run it. In a shared group or thread session the runner does not
know who sent the leftover, so the spool keeps no author and recovery declines
it with `author-missing` rather than running it as the wrong user. A
shutdown follow-up that recovery still declines (no recorded author, or older
than the 24h drain window) never runs: it gets one `Restart recovery outcome:
... outcome=skipped:<reason>:noted` line, one visible resend note to its
session origin, and its spool is quarantined with `.bad`. If no adapter can
carry the note, the spool is retained for a later boot.

## Source surfaces

`gateway/run_pending_recovery.py`, `gateway/run_restart_recovery.py`,
`gateway/run_turn.py`, `locales/*.yaml` (`followup_declined_notice`),
`gateway/platforms/base.py`, `gateway/session_transcript.py`,
`hermes_state_messages.py`, `gateway/shutdown_flush.py`, and
`tests/gateway/test_shutdown_followup_restart.py`.

## Verification

Focused restart-recovery tests cover id-less durability, spool retirement at
turn start, and pending-slot successor result propagation.

## Rollback

Revert the commit carrying `Fork-Patch: internal-followup-retirement`.
