# Auto-recovery ladder notice buffering

## Patch identity

The maintenance unit identity is ``auto-recovery-ladder-notice``.

`auto-recovery-ladder-notice`.

Fork-Patch-Backfill: 54ced3c54159aef6ec64691781641d174045b28d; auto-recovery-ladder-notice

PR #410's squash merge `4affbc6f0584` kept only its title as the message, so it
dropped the branch commit's `Fork-Patch: auto-recovery-ladder-notice` trailer.
The backfill line above records the merge's stable patch ID, which is identical
to the reviewed branch commit `82c47843`, so `main` is not rewritten.

## Required behavior

The post-exhaustion provider-recovery ladder must keep the countdown notice out of
 durable user-visible status messages while recovery is in progress. If a cycle
 eventually succeeds, the notice is dropped with other retry chatter. If the
 ladder is exhausted or the turn ends in an error, the buffered cycle notice(s)
 and final give-up line are surfaced through the existing terminal-failure flush.
 The live wait rail remains available for CLI/TUI/activity indicators, and
 `/stop` remains responsive. Logger warnings remain unchanged.

## Independent hypothesis (frozen before upstream prior-art search)

- **Observed failure:** `agent/turn_recovery_autorecover.py::auto_recover_after_exhaustion`
  calls `_emit_diagnostic_status(notice)` before sleeping, and the status callback is
  durable on gateway surfaces, so Telegram receives a persistent bubble for every
  in-progress cycle.
- **Causal chain:** the recovery countdown is emitted on the durable lifecycle rail,
  while ordinary retry/fallback chatter is already buffered in
  `StatusOutputMixin._retry_status_buffer`; success clears that buffer, but the
  ladder notice bypasses it. The exhaustion line is also emitted directly.
- **Smallest complete correction:** buffer each in-progress ladder notice with
  `_buffer_diagnostic_status`; retain `_emit_diagnostic_wait` for the live wait rail;
  buffer the final give-up line too so terminal failure flushes the complete ladder
  trace. Do not change fallback-notice behavior or logger warnings.
- **Rejected alternatives:** suppressing all diagnostic status callbacks would hide
  unrelated actionable terminal errors; changing Telegram delivery would affect
  other lifecycle statuses and leave CLI/API behavior inconsistent; removing the
  wait callback would reduce interrupt/live-wait visibility and is unnecessary if
  it is ephemeral on gateway surfaces.
- The focused auto-recovery tests prove that a cycle emits no durable status before the wait, the live wait callback still fires, terminal exhaustion flushes the buffered cycle/give-up lines, and an interrupted wait preserves the existing interrupt result.
- **Compatibility/rollback:** no state or schema change; rollback restores direct
  diagnostic status emission in the recovery module and removes its focused tests
  and maintenance record.
- **Uncertainty:** whether gateway Telegram turns `thinking.delta` into a durable
  message must be checked in current adapter wiring; the implementation keeps that
  rail unchanged pending evidence.

## Upstream status

Prior-art search is pending this independent hypothesis. No implementation change
has been made yet.

## Verification and retirement

Focused regression: `scripts/run_tests.sh tests/agent/test_turn_recovery_autorecover.py -q`.
Retire when a released upstream implementation buffers the post-exhaustion ladder
notice and preserves the same success, terminal-failure, live-wait, and interrupt
contracts.
