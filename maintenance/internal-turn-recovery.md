# Internal Turn Recovery

Fork patch identity: `internal-turn-recovery`.

Load when changing failed-turn boundaries, post-turn hooks, goal continuation, or
how synthetic gateway and CLI turns settle after an inference failure.

## Contract

A failed turn that no person started (relay, process notification, goal or loop
continuation, cron, or any event the gateway marks `internal`) must never show the human
"Your request was not processed" resend copy. Its durable failed boundary keeps empty content,
so the transcript sequence stays closed without leaking chat-facing text into the topic or into the
goal transcript.

The gateway requeues one failed internal or goal-continuation event once, with
`gateway_internal_retry_count` set to 1. It does not requeue user events, interrupted
turns (including `/stop`, `/new`, or reset invalidations), loop ticks (loop settlement
schedules the next tick), context overflow, content-policy blocks, or failures marked
non-retryable. The retry is admitted only while its invalidation epoch is current and
no user-stop latch is set, and receives fresh outbox transport/admission identity. A
second failure settles silently and does not loop.
The CLI requeues a failed synthetic continuation without running the goal judge
against the failed turn.

Human-started failed turns keep the existing visible resend notice. CLI provenance for
this distinction comes from explicit runtime markers (`TimelineNotification`, goal/loop/
heartbeat continuation stamps or exact generated prefixes), not broad notification-like
text that a user may type (including `/skill` arguments).

Contract tests: `tests/gateway/test_internal_failed_turn_recovery.py`,
  `tests/hermes_cli/test_cli_goal_interrupt.py::TestHealthyTurnStillRuns::test_failed_synthetic_continuation_is_requeued_without_judging`.
