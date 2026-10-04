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

- Extend the judge contract with bounded timed WAIT for explicit external,
  scheduled, or elapsed prerequisites when no action is available now.
- Persist a tool-evidence fingerprint, consecutive no-progress count, and
  continuation-notice identity. Park after three repeated automatic
  no-progress CONTINUE decisions with escalating waits of 5, 15, and 30
  minutes, resetting on recorded progress or a real user turn.
- Re-arm live pid/session barriers with the same 30-minute hard interval cap and
  escalating liveness-probe delays. The pid/session identity stays authoritative;
  a process exit or watch-pattern match lifts the barrier. The existing factual
  lift note remains the single defensive status line for an unexpected live
  target lift.
- Old goal rows load with zero/empty defaults; no schema migration is needed.

## Verification

Focused regression: `tests/hermes_cli/test_goal_external_wait_backoff.py` and
`tests/hermes_cli/test_goals.py::TestWaitBarrier`. The new live-barrier test
fails on the pre-fix implementation because the barrier is cleared after the
30-minute cap, then passes with the re-arm behavior. The focused suite passes
with the current candidate.

Run the affected goal and gateway modules plus the repository canonical gate
before publication. No runtime promotion or restart is part of this patch.

## Retirement and rollback

Retire when a released upstream implementation satisfies the complete contract
and passes the focused regressions. Rollback is a source revert of this patch's
files; the added durable fields are optional and old rows remain readable.
