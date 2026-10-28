# Supervisor Check-ins

## Preserve

Automatically supervise gateway-owned background work without occupying a user loop or heartbeat slot. Keep the state on the compression lineage, deliver to the proven original topic/profile/bot, and never wake a new or forked conversation for its predecessor's work. Only acknowledged visible output resets the cadence. No outstanding work means no check-in turn or status message.

The `supervisor_checkin` display kind is generated machinery with a mandatory written status. It is the bounded exception to unchanged-wait silence, not permission to make ordinary goal or loop wakeups chatty. Reaction-only acknowledgement does not satisfy it. The existing one-shot gateway silence continuation owns model recovery, and normal final delivery owns the factual fallback, never a second transport send path.

A user's queued message, completion, existing machinery input, `/stop`, emergency stop, and runtime draining take priority. Validate work and the due identity again at execution, not only when scanning. Observation failures retain the candidate and log the failure, rather than converting unknown state to completion. Reconcile external effects before recovery or restart.

## Provenance And Patches

- **Identity:** `supervisor-checkins`, active fork feature.
- **Baseline:** maintained fork `origin/main`, `1d587b5821e6ad9b49158a1a70886db757a3b089`.
- **Why Core:** idle gateway turn completion, native automation admission, structured provenance, and required final delivery are core boundaries. An opt-in plugin timer cannot safely guarantee this contract for every owned delegation/process.
- **Prior Art:** closed, unmerged upstream [#22052](https://github.com/NousResearch/hermes-agent/pull/22052) proposed a gateway check-in scanner but deferred live prompt injection. The feature retains completion push as the fast path and adds bounded supervision as fallback. It does not replace the active-turn notification interval.
- **Related Units:** [Internal notification silence](internal-notification-silence.md), [Gateway silence re-prompt](gateway-silence-reprompt.md), [Native admission metadata](native-admission-synthetic-metadata.md), and [Gateway state DB guard](gateway-loop-statedb-guard.md).
- **Delivery:** commits carrying `Fork-Patch: supervisor-checkins`. Protected fork landing requires exact-head qualification and independent review. Source delivery does not activate Hermes.

## Update And Retirement

Compare upstream idle admission and task supervision before porting this patch. Adopt an upstream implementation only when it covers automatic arming, durable timer state, execution-time cancellation, original topic routing, mandatory bounded written updates, and completion disarming together. A heartbeat or progress notifier alone is not a replacement. Keep explicit cross-session dependency declaration outside this unit until a real case requires it.

## Verification

Run `tests/hermes_cli/test_checkins.py`, `tests/gateway/test_supervisor_checkins.py`, and `tests/gateway/test_supervisor_checkin_daemon.py`, then affected heartbeat, goal/loop, silence-recovery, synthetic-prompt and native-automation suites. The daemon proof starts an isolated gateway with a real notify-enabled terminal process and loopback relay/model. It checks durable admission, marker recovery, original topic delivery and completion disarming. Use the repository exact-head gate before protected landing. Disable `checkins.enabled` or revert this unit to remove automatic supervision without altering existing loops, goals or completion delivery.
