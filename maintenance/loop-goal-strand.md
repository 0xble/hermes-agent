# Loop–Goal Stranding Repair

Fork patch identity: `loop-goal-strand`.

A goal continuation judge runs in deferred post-delivery bookkeeping. The adapter joins that bookkeeping for only `POST_TURN_DELIVERY_WAIT_S` (60 seconds); if the auxiliary judge outlives the join, the adapter releases its session guard before the judge enqueues the continuation. `_enqueue_fifo` records the synthetic event but historically did not start a drain, so the event remained in the pending slot until unrelated ingress arrived. Because the active goal was still non-parked, `goal_blocks_loop_tick()` deferred the loop tick too: one lost drain stranded both automations.

The 2026-11-24 strand had loop tick #1043 admitted at 03:58:04 and its 398-second turn ending at 04:04:44. The successful judge decision updated `last_automatic_continuation_at` around 04:05, but no continuation ran until the 08:43 relay injection; the 08:48:46 loop wake was #1044. The fix starts the normal adapter drain when a continuation enqueue finds the key idle, while preserving the guard check so an in-flight successor is never duplicated.

Contract tests: `tests/gateway/test_goal_continuation_drain.py::test_idle_post_turn_goal_continuation_starts_a_new_fifo_drain` (fails before the fix, passes after it).
