# Gateway stop stays stopped

Load this unit when changing gateway `/stop`, `_interrupt_and_clear_session`,
async-delegation or process-completion injection, or goal pause/revival.

## Required behavior

- `/stop` on a messaging gateway pauses the session's standing goal with reason
  `user-interrupted (/stop)` and drops queued synthetic goal continuations,
  matching the CLI's Ctrl+C. This applies mid-turn, while the agent is still
  starting, and between turns. A judge `BLOCKED` pause is overwritten, so the
  next ordinary user message does not revive the goal. Only `/goal resume` does.
- `/stop` sets a per-conversation stop latch before it interrupts anything.
  While the latch is set, async-delegation completions, process completions and
  watch-pattern wakes for that session stay queued. They are not injected as a
  turn, and holding them takes no durable claim and spends no delivery attempt.
- The latch is durable. Besides the in-memory conversation flag it is persisted
  as `stop_latched` on the session's routing entry (`SessionEntry`, stored in
  the existing `gateway_routing` rows / `sessions.json`; an additive JSON field
  older releases ignore). Both the memory and durable paths validate the owning route.
  Stop captures the routing entry before yielding: compression changes its id in place,
  while reset/resume replaces it. This preserves a hold across compression but never
  grants a replaced route's hold to a successor. After a gateway restart, held
  completions stay held until the user sends a turn. Completions
  pinned to compression ancestors resolve their current conversation owner via
  `GatewayNotificationsMixin._resolve_compression_lineage_target`, the same
  live-tip/route-lineage resolver used by notification retargeting. A lineage
  ending at `/new` or reset does not inherit the successor's hold.
- The latch clears when the next turn the user sent is admitted (not internal,
  not a goal continuation, heartbeat or relayed message), both in memory and
  on disk. Held completions are then delivered as usual. `/new` and other
  conversation boundaries clear it too: the successor routing entry starts
  unlatched and a hold owned by a replaced session id never applies. Store lookup and
  persistence run off-loop; monotonic latch revisions reject an older write after a
  newer stop/clear. Admission captures and clears all routing aliases before yielding.
- The goal pause runs after the stop's awaits, so the stopped session id and
  run generation are captured before interrupt awaits and the pause is skipped
  when a concurrent `/new` or newer turn owns the route by then. Cold pending routing
  loads off-loop while its slot remains claimed, with a generation check before adopting
  the loaded owner (normal busy-command dispatch and the pending-only path). Warm-up and
  executor admission can both yield after the early fence: recheck at the goal
  mutation inside the executor. The routing lock remains off-loop, while the
  generation lock covers only a short in-memory check, NEVER goal/store I/O.
  `hermes_cli/goals_pause.py` acquires SQLite's writer before that final ownership
  check and CAS-validates the loaded goal snapshot. The transaction orders the
  pause ahead of successor goal writes while generation admission remains nonblocking.
  Lock order is route store, goal DB, short generation check. Compression may advance
  the same stopped entry to its live tip; a reset/new owner may not receive the pause.
- Between-turn `/stop` replies "Stopped" when it paused a goal, not only when
  it interrupted background delegations.

## Why

On 2026-10-02, a Telegram `/stop` killed the turn and four background PR-review
delegations. Their interrupted completions arrived one second later and started
a new turn. The goal judge then queued two continuations before it judged the
goal blocked, and the agent sent three unrequested replies after the stop.
`gateway/slash_commands.py` and `gateway/run_busy.py` never touched the goal,
and the notification path never checked for a stop.

## Provenance

Fork patch identity: `gateway-stop-stays-stopped`. Own fork patch.

Upstream comparison on 2026-10-02:

- [PR #126960](https://github.com/NousResearch/hermes-agent/pull/126960)
  (merged 2026-09-29, fixes #124347) adds the same latch to the TUI/Desktop
  backend only (`tui_gateway`). Messaging gateways are not covered.
- [PR #74974](https://github.com/NousResearch/hermes-agent/pull/74974) (open
  since 2026-07-30, no review decision) pauses goals on gateway `/stop`, but
  targets the pre-split `gateway/run.py`. It also adds provider-failure
  heuristics. It does not hold the delegation completions that `/stop`
  produces, and those completions were the trigger in this incident.

## Verification

Run `scripts/run_tests.sh tests/gateway/test_stop_pauses_goal_and_holds_wakes.py
tests/gateway/test_stop_loop_responsiveness.py`. The regressions exercise durable
parent-id delegations after compression/stop/restart; reset rejection BEFORE and
after restart; ownership changes at warm-up/executor admission; and suspended
goal/store writes. A real loop-thread generation claim must leave a 50 ms heartbeat
under 200 ms even while stop persistence is held on a threading event. Newer goal
writes serialize at SQLite instead of blocking generation claims. Same-generation
goal edits survive the snapshot CAS. Delayed latch set/clear follows compression,
rejects reset and stale revisions, and respects grouped alias admission.
Waits are event-based and bounded. Also run
`tests/gateway/`, `tests/hermes_cli/test_goals.py`, and
`tests/tools/test_async_delegation.py`.

## Retirement and rollback

Retire when a selected upstream release pauses the goal on gateway `/stop` and
holds stop-produced completions until the next user turn, and this regression
passes without the local code. To roll back, revert the patch commit. The durable
latch is one extra boolean in the routing entry JSON that older code ignores,
and the pause reason is an ordinary goal row value, so nothing needs to be
migrated.
