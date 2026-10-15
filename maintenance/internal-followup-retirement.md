# Internal recovered follow-up retirement

Fork patch identity: `internal-followup-retirement`.

## Load when

Changing shutdown follow-up spooling, restart recovery admission or retirement,
or the durable-input, decline, and visible-note paths of recovered follow-ups.

## Behavior

Shutdown follow-ups without a platform message id (internal, string, or any
id-less `MessageEvent`) get a synthetic recovery id in a recovery-only field at
flush; it is persisted as the user row's dedupe value and never exposed as
transport identity. Recovery uses that id to detect a durable turn-start row, so
a crash between transcript persistence and spool unlink cannot replay the
prompt. Only true legacy spools (no message id and no recovery id) fall back to
exact content plus spool timestamp. On a match while the session's own
`resume_pending` marker is being auto-resumed, that resume owns the turn and the
spool is dropped without a note (a resend could repeat its tool effects).
Otherwise the fallback never deletes: a match is declined as
`possibly-duplicate` (one outcome, one visible note that it may already have
been handled and to resend only if not, spool quarantined), and without a spool
timestamp it never matches. When a recovered
event is merged into a busy adapter's pending slot, the background task records
the event it actually consumes and restart recovery carries that task's terminal
result back to the parked recovery event. The recovery outcome is then `resumed`
or `skipped:turn-failed`, rather than `accepted-without-turn`. If the turn
raises after its input row is durable (so the spool may already be retired), the
outcome is `skipped:turn-failed:noted` (or `:note-unavailable` when the send
fails) with the failed-turn note, never the "still preserved" notice. Notice
text is translated inside the event's profile scope so a multiplexed secondary
profile gets its own locale.

A draining turn's leftover `/steer` or interrupt text is only a string. In a DM
or per-user session the shutdown follow-up spool binds it to the turn's source
(author, chat) and a recovery id before writing, so restart recovery can
attribute and run it. In a shared group or thread session the runner does not
know who sent the leftover, so the spool keeps no author and recovery declines
it with `author-missing` rather than running it as the wrong user. A
shutdown follow-up that recovery still declines (no recorded author, possible
duplicate, or older than the 24h drain window) never runs: it gets one
`Restart recovery outcome: ... outcome=skipped:<reason>:noted` line, one visible
resend note to its session origin, and its spool is quarantined with `.bad`. If
no adapter can carry the note, the spool is retained for a later boot.

## Source surfaces

`gateway/run_pending_recovery.py`, `gateway/run_restart_recovery.py`,
`gateway/run_turn.py`, `locales/*.yaml` (`followup_declined_notice`,
`followup_possible_duplicate_notice`),
`gateway/platforms/base.py`, `gateway/session_transcript.py`,
`hermes_state_messages.py`, `gateway/shutdown_flush.py`,
`tests/gateway/test_shutdown_followup_restart.py`, and
`tests/gateway/test_proxy_mode.py`.

## Verification

Focused restart-recovery tests cover id-less durability, recovery-id stamping
for every id-less spool, legacy duplicate-content decline and quarantine versus resume-owned drop,
in-scope notice translation, spool
retirement at turn start, the post-retirement turn-failed path, and pending-slot
successor result propagation.

## Rollback

Revert the commit carrying `Fork-Patch: internal-followup-retirement`.
