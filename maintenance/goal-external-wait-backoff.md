# External-wait goal backoff

## Fork patch identity

This maintenance unit owns the fork patch identity `goal-external-wait-backoff`.

## Required behavior

A standing goal must park when its next progress depends on work outside the
current session, including an external service, cron/watchdog, scheduled time,
or elapsed hold, when the current response has no actionable step. Repeated
automatic turns with no recorded progress must use durable escalating timed
backoff rather than busy-polling. A pid/session barrier whose target is still
running must re-arm after its bounded probe window instead of lifting and waking
the agent; exit or the process session's watch trigger remains the wake
condition.

## Independent hypothesis and upstream comparison

The frozen hypothesis is recorded at
`~/.hermes/cache/scratch/goal-busy-poll-hypothesis-20261004.md`. The native
judge prompt omitted external prerequisites, so it defaulted to CONTINUE, and
pid/session barriers treated the 30-minute probe cap as permission to resume
judging even when the target was still alive. A core patch is required because
this behavior spans judge semantics, durable goal state, barrier liveness, and
idle wake admission; instructions or a plugin cannot atomically own those
boundaries.

Related upstream PRs reviewed before implementation: #106925, #118705,
#129380, and #107130. None supplied a released equivalent for the complete
external-wait, no-progress, and live-barrier contract. The selected fork
implementation remains intentionally narrow and keeps CONTINUE for actionable
work.

## Design

### Turn provenance and no-progress backoff

`evaluate_after_turn` receives an explicit `user_initiated` signal; it does not
infer provenance from assistant prose, timing, or queue contents. The gateway
already knows `is_internal` and marks the synthetic goal-continuation event, so
`_post_turn_goal_continuation` passes `user_initiated=False`. The CLI and TUI
post-turn hooks likewise pass the provenance of the turn they just completed:
real admitted input is `True`, while goal continuation, idle wake, and internal
notification turns are `False`. This keeps the no-progress counter on the real
production paths instead of relying on direct test calls with a default.

The counter fingerprints only stable evidence from the current turn. Quality-
gate
rows are excluded from that fingerprint (their pass output is still supplied to
the judge), and the read-only-status test examines only rows added by this turn.
Three consecutive synthetic CONTINUE decisions with the same reason and no new
progress park the goal with escalating bounded timed waits; any new evidence or
real user turn resets the streak.

### Live-barrier lifetime

A live pid/session target is the authoritative wake source. While it is alive,
keep the barrier armed and re-arm its next liveness check with escalating
backoff; do not wake the agent merely because a 30-minute probe window elapsed.
The first time a wait exceeds 30 minutes, emit one user-visible notice, deduped
by S1's durable `last_wait_notice_key`. Use a separate `barrier_recheck_at`
deadline so `waiting_until` retains its existing timed-wait meaning and pid/
session readers continue to show the target. At six hours, pause the goal with a
clear blocker notice naming the still-live pid/session. If the target has exited,
clear the barrier and resume promptly, including the existing receipt-based
restart/idle-wake path.

`lifted_barrier_prompt` remains pure: it only reads state and returns a prompt.
Any re-arm persistence uses a CAS-style conditional update keyed by the original
`waiting_since` (the same pattern as `clear_goal_wait_if_since`), so a concurrent
pause, clear, or re-park wins rather than being overwritten by a blind save.

Old goal rows load with zero/empty defaults; no schema migration is needed.

### Test matrix

| Behavior | Regression test |
| --- | --- |
| Synthetic production continuation counts as automatic | `test_gateway_goal_continuation_uses_synthetic_provenance` |
| Three no-progress turns park with escalating backoff | `test_three_qualifying_no_progress_turns_back_off_and_persist` |
| 30-minute live-wait notice is emitted once | `test_live_barrier_emits_one_age_notice` |
| Six-hour live wait pauses with target named | `test_live_barrier_pauses_at_hard_ceiling` |
| CAS loss leaves a concurrent re-park intact | `test_live_barrier_rearm_respects_cas_loss` |
| Exited targets resume promptly | `test_restart_killed_process_lifts_barrier_with_a_factual_note`, `test_untracked_process_reports_unknown_outcome` |
| Idle wake remains pure and clear is conditional | `test_clear_lifted_wait_respects_a_newer_repark_or_pause` |
| Stable evidence excludes quality-gate timestamps and old rows | `test_quality_gate_rows_do_not_reset_no_progress` |
| Read-only classification is scoped to this turn | `test_read_only_status_only_uses_new_rows` |
| Judge prompt and command safety are correct | `test_judge_prompt_allows_external_scheduled_wait`, regex unit coverage |

## Verification

Focused regression: `tests/hermes_cli/test_goal_external_wait_backoff.py` and
`tests/hermes_cli/test_goals.py::TestWaitBarrier`. The new live-barrier tests fail on the pre-fix implementation because the barrier
was cleared after the 30-minute cap, then pass with the separate recheck deadline,
CAS re-arm, 30-minute notice and six-hour pause behavior. The focused suite passes
with the current candidate.

Run the affected goal and gateway modules plus the repository canonical gate
before publication. No runtime promotion or restart is part of this patch.

## Retirement and rollback

Retire when a released upstream implementation satisfies the complete contract
and passes the focused regressions. Rollback is a source revert of this patch's
files; the added durable fields are optional and old rows remain readable.
