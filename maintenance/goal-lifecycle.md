# Goal judging and blocker recovery

## Required behavior

The native judge receives the complete goal, completion contract, and additive
subgoals. Only the assistant-response preview is bounded. Admitted real user
input revives a judge-blocked goal before model work without resetting its budget.
Explicit pauses, interrupts, exhausted budgets, failed judges, unauthorized input,
hidden messages, relayed agents, and synthetic notifications do not revive it.
Failed or interrupted model turns do not run completion judging.

## Provenance and adaptation

- Fork patch identity: `slice-3-goal-lifecycle`. The original goal lifecycle slice: `goal_set`
  honors the profile budget and surfaces agent `goal_set` receipts as notices.
- Fork patch identity: `goal-criteria-completeness`. Own upstream contribution:
  [PR #118343](https://github.com/NousResearch/hermes-agent/pull/118343),
  tracked by [issue #118334](https://github.com/NousResearch/hermes-agent/issues/118334).
  Fixes all three authoritative-input truncation sites. Related response-excerpt
  PR #70701 is intentionally separate and does not fix missing criteria.
- Fork patch identity: `goal-blocker-semantics`. Native judge BLOCKED covers both
  impossible outcomes and recoverable external dependencies; neither is DONE.
  Prompt judgment prefers available authorized work before pausing, while the
  pause reason and notice no longer call every block unachievable. Old blocked
  pause records still resume on admitted real user input. Archived provenance:
  HERMES-071 (goals-and-turn-authority); upstream contribution
  [PR #123859](https://github.com/NousResearch/hermes-agent/pull/123859)
  at `222b6591a0c0`, not yet released. Regression:
  `scripts/run_tests.sh -j 6 tests/hermes_cli/test_goal_resolvable_blocker.py
  tests/hermes_cli/test_goals.py`; retire when the selected release passes this
  proof without the local prompt/notice adaptation. Rollback reverts only this
  identity's prompt, pause-label and compatibility changes, preserving blocked
  goal state and the independent recovery behavior.
- Fork patch identity: `goal-blocked-recovery`. Adopted source:
  [PR #104380](https://github.com/NousResearch/hermes-agent/pull/104380), head
  `87bdf180e17edad1eff2a60b71a771660bbe0008`, authored by Zeus-Deus.
  The PR is open, not upstream-accepted. Preserve all goal recovery surfaces,
  including queued/isolated TUI input and Desktop state display. Exclude unrelated
  batch-clarify compaction changes. Adapt moved test helpers and the release's
  framed-string delegation notifications, which predate SubagentNotification.
  Resolve methods_prompt against the release version without importing unrelated
  upstream refactors.

- Fork patch identity: `goal-pause-race`. Evaluation runs on an isolated snapshot and atomically
  compares that snapshot with the durable row before committing any verdict. Every durable
  mutation carries a fresh token, including pause/resume cycles returning to equal values.
  Stale evaluations and failed persistence never authorize continuation. Commands remain
  authoritative before evaluation, during gates/judging, and at the final write boundary.
  Our upstream [PR #124017](https://github.com/NousResearch/hermes-agent/pull/124017)
  now carries this complete atomic settlement and mutation-token contract.
  Regression: `scripts/run_tests.sh tests/hermes_cli/test_goal_evaluation_atomic.py
  tests/hermes_cli/test_goals.py tests/hermes_cli/test_goal_gates.py`.

- Fork patch identity: `goal-judge-evidence`. Own fork patch, no upstream PR yet.
  The judge receives an evidence ledger: up to 8 recent non-bookkeeping tool
  results recorded since the goal was set (call, secret-redacted output tail,
  age), plus every quality gate that just passed. Evidence gathered with tools
  counts without the agent pasting it into prose. Verification items no command
  can prove are satisfied by the response stating them. A CONTINUE verdict may
  carry `disputed: true` when the agent asserts completion the judge rejects;
  two in a row pause the goal with `judge disputed completion:` so the user
  chooses `/goal clear` or `/goal resume`. A disputed pause is not revived by
  ordinary user input. The TUI passes the agent transcript id because its goals
  are keyed by session key. Prompts without evidence stay byte-identical.
  Related upstream: issue #70699 and PR #70701 fix response truncation only.
  Regression: `scripts/run_tests.sh -j 6 tests/hermes_cli/test_goal_judge_evidence.py`,
  red without the patch. Rollback reverts the source change; old goal rows load
  with a zero dispute counter and no schema change.

- Fork patch identity: `goal-adaptive`. Extends `goal-judge-evidence`. Own fork
  patch, no upstream PR yet. Four parts:
  - **Cited evidence.** Identifiers the response cites (backtick spans, quoted
    strings, SHAs, URLs, long ids, `N passed`) are located verbatim with
    `instr` in every tool result recorded since the goal was set, including
    compaction-archived rows. A cited command resolves to its own result. A
    runtime delegation or background-process notice counts, labeled as such.
    Agent prose, ordinary user text and bookkeeping tools never count. The
    judge sees the redacted excerpts plus a list of citations that were not
    found, which it treats as unproven.
  - **Response window.** The judge sees the head and the tail of a long
    response, so a closing Evidence section is not cut off.
  - **Disputes.** A dispute names one missing criterion and the check that would
    prove it. A dispute counts toward the stall breaker, which pauses at 3
    instead of 2, unless the reply cites a recorded result that no earlier dispute
    in the streak cited. Rewording or dropping citations is not progress.
  - **Revisions.** `GoalManager.revise()` records a versioned revision (actor,
    reason, user quote with its source message, before, after). The judge prompt
    shows every revision and every requirement it replaced, in full. The
    continuation prompt shows the current version. Changing the objective or
    constraints, or dropping a subgoal, needs a verbatim quote of 12+ characters
    from a real user message sent since the goal was set. The runtime proves only
    that the user said it. The judge sees the complete source message, which may
    be at most 4,000 characters (longer ones are refused, never excerpted), and
    decides whether it plainly instructs the specific change. Otherwise it holds
    the agent to the earlier requirement.
  The judge prompt judges the end state, not the route. A remedied state
  invariant stops blocking `done`, while a breached irreversible prohibition
  returns BLOCKED.
  Regression: `scripts/run_tests.sh tests/hermes_cli/test_goal_adaptive.py`.
  Live replay of two historical false-negative disputes against the real judge
  is recorded in the PR. Rollback reverts the source change. `revisions` and
  `last_dispute_evidence` default empty on old rows, and there is no migration.

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
goal migration is introduced. The optional mutation token defaults empty on older rows.
Landing is not runtime promotion or activation.

The v2026.9.21 integration preserves trusted user-turn admission across upstream
prompt metadata and compute-host changes. Title previews remain presentation data,
and hidden or relayed turns do not acquire user authority.
