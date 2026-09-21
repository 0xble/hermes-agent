# Goal judging and blocker recovery

## Required behavior

The native judge receives the complete goal, completion contract, and additive
subgoals. Only the assistant-response preview is bounded. Admitted real user
input revives a judge-blocked goal before model work without resetting its budget.
Explicit pauses, interrupts, exhausted budgets, failed judges, unauthorized input,
hidden messages, relayed agents, and synthetic notifications do not revive it.
Failed or interrupted model turns do not run completion judging.

## Provenance and adaptation

- Fork patch identity: `goal-criteria-completeness`. Own upstream contribution:
  [PR #118343](https://github.com/NousResearch/hermes-agent/pull/118343),
  tracked by [issue #118334](https://github.com/NousResearch/hermes-agent/issues/118334).
  Fixes all three authoritative-input truncation sites. Related response-excerpt
  PR #70701 is intentionally separate and does not fix missing criteria.
- Fork patch identity: `goal-blocked-recovery`. Adopted source:
  [PR #104380](https://github.com/NousResearch/hermes-agent/pull/104380), head
  `87bdf180e17edad1eff2a60b71a771660bbe0008`, authored by Zeus-Deus.
  The PR is open, not upstream-accepted. Preserve all goal recovery surfaces,
  including queued/isolated TUI input and Desktop state display. Exclude unrelated
  batch-clarify compaction changes. Adapt moved test helpers and the release's
  framed-string delegation notifications, which predate SubagentNotification.
  Resolve methods_prompt against the release version without importing unrelated
  upstream refactors.

The initial reproduction established missing criteria and paused state after a
repair turn. Upstream comparison confirmed both and supplied a matching recovery
implementation. Configuration or plugin changes cannot repair these native judge
and admission boundaries. No new tool, permission, or provider is introduced.

Any admitted real user turn can revive a blocker pause, including a question about
the blocker. This adopts upstream's conversational recovery policy, not semantic
proof that the blocker is solved. The next judge may block again. Explicit user
pauses remain authoritative.

## Verification

Run scripts/run_tests.sh for tests/hermes_cli/test_goal_criteria_completeness.py,
tests/hermes_cli/test_goals.py, tests/gateway/test_goal_verdict_send.py,
tests/hermes_cli/test_blocked_goal_turn_admission.py, tests/tui_gateway/test_goal_command.py,
and tests/tui_gateway/test_tui_gateway_queue_on_busy.py. Also cover the existing
goal dispatch, wait, restart, budget, notice, and quality-gate regression suites.
Desktop: the session-control.test.tsx UI suite.

Gateway tests enter the real GatewayRunner message path with Telegram session
identity, isolated SQLite state, a controlled agent result, and recording transport.
They prove recovery before execution, notices and continuation queueing, plus
negative admission and failure paths. They do not contact Telegram or a live LLM.

## Retirement and rollback

Retire each patch when the accepted upstream release includes equivalent behavior
and passes its regressions. Revert its source change to roll back. No schema or
goal migration is introduced. Landing is not runtime promotion or activation.

The v2026.9.21 integration preserves trusted user-turn admission across upstream
prompt metadata and compute-host changes. Title previews remain presentation data,
and hidden or relayed turns do not acquire user authority.
