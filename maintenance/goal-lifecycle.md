# Goal judging and blocker recovery

## Required behavior

The native judge receives the complete goal, completion contract, and additive
subgoals. Only the assistant-response preview is bounded. Admitted real user
input revives a judge-blocked goal before model work without resetting its budget.
Explicit pauses, interrupts, exhausted budgets, failed judges, unauthorized input,
hidden messages, relayed agents, and synthetic notifications do not revive it.
Failed or interrupted model turns do not run completion judging. The autonomous
silent-recheck streak resets only on a real break in silence: a user-initiated
turn, a visible autonomous reply, new eligible non-read-only evidence, or a DONE
or BLOCKED verdict. Entry resets precede every wait or gate return. An autonomous
silence marker increments it only when the judge returns CONTINUE, including a
disputed completion claim, with no new eligible non-read-only evidence. Nonjudging
pacing, backoff, parked and delegation-wait exits, silent quality-gate failures,
and judge transport/parse failures leave it unchanged. The third counted judgment
clears the goal once with one short notice, before dispute pause or timed backoff.

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

- Fork patch identity: `goal-capability-evidence`. Corrects the shared exclusions
  used by `goal-judge-evidence` and `goal-adaptive`: capability discovery,
  installation and readback results can prove the goal's outcome. `skill_view`,
  `skills_list`, `skill_manage`, `tool_search` and `tool_describe` reach the bounded,
  redacted recent ledger and citation lookup, including archived results. Merely
  reading/discovering a capability remains read-only for no-progress detection;
  `skill_manage` remains actionable. Goal/memory bookkeeping stays excluded.
  This fixes a skill-deployment goal whose judge repeatedly requested a successful
  load that the evidence filter hid, even when full (not deduplicated) results
  existed. No forced DONE, pause, budget, delivery or prompt change is introduced.
  Regression: `scripts/run_tests.sh tests/hermes_cli/test_goal_capability_evidence.py
  tests/hermes_cli/test_goal_judge_evidence.py tests/hermes_cli/test_goal_adaptive.py`.
  Retire when the selected upstream passes the same recorded-capability proof;
  rollback restores only these evidence/read-only classifications. No schema or
  live-state migration is needed.

- Fork patch identity: `goal-done-repeat-silence`. Own fork patch for PR #531. Autonomous continuation replies distinguish the first done/blocker report from an unchanged repeat: the first done claim remains visible and must include exact Evidence; an unchanged done claim or unchanged blocker/input need must be exactly `[SILENT]`. Changed state, new evidence, or a changed blocker makes the next report visible again. Silent repeats still reach `judge_goal()`. On a silent autonomous recheck, the runtime passes the newest visible final report in this goal's SessionDB history (since `created_at`, at most 500 assistant rows) verbatim, excluding hidden rows, compression summaries and tool-call commentary. No completion-phrase recognizer remains. The per-turn judge input labels it as the agent's last visible report and applies the existing 4,000-character head plus 3,000-character tail bound; report text never enters the stable system prefix. Citations resolve from the full report through the existing lifetime-bounded evidence finders, including proof outside the eight-row ledger and citations in an omitted middle. Plain, subgoal and contract judges decide whether the report claims and proves completion. A newer non-completion report replaces the older done report and its citations by construction. Prose is not proof, and no eligible visible report leaves the original silent judge input unchanged.
  Three autonomous silent CONTINUE or disputed verdicts without a real break in silence and with no new eligible non-read-only evidence rows clear the goal through the convergence-breaker path and emit one short `Goal stopped` notice. This uses the existing no-progress threshold (`DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS = 3`) and terminal `cleared` status, not a user-resumable pause or a rearmed timer. The persisted streak resets only on new eligible non-read-only evidence, user-initiated turns (including silence markers), visible autonomous replies, DONE or BLOCKED. These facts reset at evaluator entry or when the verdict becomes known, before early returns. Read-only/status-only evidence does not reset it. Pacing/continuation-gap, timed-backoff/no-progress, parked and delegation-wait exits that do not judge, silent quality-gate failures, and transport/parse failures preserve it. The judged increment is persisted even when pacing or backoff prevents immediate dispatch; the terminal silent-stall rule takes precedence over dispute pause and timed no-progress backoff. Later stale turns, restarts and user messages do not replay the stop notice. Existing visible dispute pauses remain unchanged; silent disputes use this non-blocking backstop. The existing gateway notice path delivers the terminal notice; its `✗` prefix makes the shared classifier treat it as a protected outcome, so daily-quota shedding cannot drop the only record that the goal ended. No delivery-level duplicate filter or gateway source change is needed. Regression: `scripts/run_tests.sh tests/hermes_cli/test_goal_judge_evidence.py tests/gateway/test_goal_continuation_notice_dedupe.py` covers plain/subgoal/contract newest-report input, emoji/heading/bullet and ordinary completion reports with citations older than the ledger across silent repeats, newer failed-verification reports replacing older done reports, bounded head/tail with omitted citations still resolved, no-visible-report input equivalence, goal-lifetime isolation, real-break resets before failing gates or waits, unchanged nonjudging exits, and one-notice terminal convergence under the gateway default 900-second continuation gap with expired timers advanced. Retire when upstream supplies the same first-visible/unchanged-silent reply contract, newest-visible-report citation handling without phrase matching and bounded non-blocking stall behavior; rollback reverts this identity's prompt, judge-input, counter and tests, with old serialized rows defaulting the silent counter to zero.

- Fork patch identity: `goal-evidence-toggles`. Goal continuation prompts allow a collapsed `<details><summary>Evidence</summary>...</details>` block where the platform renders toggles, with the outcome visible above it and exact identifiers/output lines still quoted verbatim in backticks. The rich-message Telegram hint (`TELEGRAM_RICH_MESSAGES_HINT`, appended only when `platforms.telegram.extra.rich_messages` is enabled) folds dense secondary content (evidence, logs, ID/SHA lists, long verification detail and diffs), not short answers. Toggles render only on the rich-message path; the default MarkdownV2 path and rich fallbacks (CJK, math inside details, oversize, missing endpoint) would show raw tags, so the guidance must stay rich-gated. Citation extraction and ledger lookup continue reading the raw response; no renderer or judge policy change is needed. Upstream checked at `e06df9c2eae6901f4c88f464e0ae6e753c0428b8`: related open PRs [#2843](https://github.com/NousResearch/hermes-agent/pull/2843) and [#7369](https://github.com/NousResearch/hermes-agent/pull/7369) add different transport/reasoning collapse mechanisms and are not adopted. Regression: `scripts/run_tests.sh tests/hermes_cli/test_goal_adaptive.py tests/hermes_cli/test_loops.py tests/gateway/test_telegram_rich_messages.py`. The adaptive tests prove backticked identifiers inside `<details>` reach the judge even when the matching tool result is outside the recent evidence window. Retire when the selected upstream has equivalent prompt guidance and passes these tests; rollback reverts this identity's static prompt strings and tests, with no state migration.

- Fork patch identity: `unlimited-goal-budget`. `goals.max_turns: 0` is a durable unlimited sentinel: config consumers preserve zero, serialized and migrated goal rows round-trip it, and the standing-goal continuation checks skip only the finite budget pause. Kanban task budgets retain their existing single-shot and finite semantics. Done, blocked, judge-failure, dispute, wait, pause, resume, clear, and positive finite budgets remain unchanged. Regression: `scripts/run_tests.sh tests/hermes_cli/test_goals.py tests/gateway/test_goal_max_turns_config.py tests/tui_gateway/test_goal_command.py`. Retire when the selected upstream release preserves zero through the same surfaces and passes this proof; rollback reverts this identity's normalization, budget guards, notices, docs, and tests.

- Fork patch identity: `goal-adaptive`. Extends `goal-judge-evidence`. Own fork
  patch, no upstream PR yet. Four parts:
  - **Cited evidence.** Identifiers the response cites (backtick spans, quoted
    strings, SHAs, URLs, long ids, `N passed`) are located with `instr` in every
    tool result recorded since the goal was set, including compaction-archived
    rows. A trailing `…` or `...` resolves by prefix only when at least 8
    characters remain; a commit URL ending `/commit/<7–40 hex>` can resolve by
    its hash in a tool result, explicitly labeled as hash-matched rather than
    URL-verified. The judge sees the real matched excerpt. A cited command
    resolves to its own result. A runtime delegation or background-process
    notice counts, labeled as such. Agent prose, ordinary user text and
    bookkeeping tools never count. Missing prefixes and hashes remain unresolved;
    the judge treats those citations as unproven.
  - **Response window.** The judge sees the head and the tail of a long
    response, so a closing Evidence section is not cut off.
  - **Disputes.** A dispute names one missing criterion and the check that would
    prove it. A dispute counts toward the stall breaker, which pauses at 3
    instead of 2, unless the reply cites a recorded result that no earlier dispute
    in the streak cited. Rewording or dropping citations is not progress.
  - **Revisions.** `GoalManager.revise()` records a versioned revision (actor,
    reason, user quote with its source message, before, after). The judge prompt
    shows every revision and every requirement it replaced, in full. The
    continuation prompt shows the current version and the same history, with each
    replaced requirement still binding unless its cited user message plainly
    instructs the change, so the working agent cannot act on an unauthorized
    weakening before the judge sees it. Changing the objective or
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

- Fork patch identity: `goal-continuation-gap`. Own fork patch, no upstream PR yet.
  Two autonomous-wake controls. (1) Gateway GoalManagers pace autonomous
  continuations: after a turn with no fresh evidence (continuation, `/loop`,
  heartbeat), the next continuation waits until `goals.min_continuation_gap_seconds`
  (default 900, `0` disables) after `GoalState.last_continuation_at`, stamped on
  every judge CONTINUE and on the idle ticker's CAS clear. The hold is the ordinary
  durable timed wait with reason `minimum gap between autonomous goal continuations`;
  it is silent (no parked notice). Every idle-ticker resume is silent: the
  continuation turn is the visible signal, so no wait-ended notice is sent. A user turn or an event with
  `notification_origin=process_registry_synthetic` (also carried through queued
  follow-up chains as `queued_terminal_notification_origin`) pierces only this hold;
  judge waits and pid/session/delegation barriers stay authoritative. CLI/TUI
  managers keep the gap off. (2) Completion fan-in: a successful process or
  delegation result for a conversation that is mid-turn or started a turn within
  `gateway.completion_notification_batch_window_seconds` (default 300, capped 3600,
  `0` = same-tick 0.1s fan-in only) is held and delivered as one synthetic turn.
  Failures, non-zero or abnormal exits, failed delegations and results a parked
  goal waits on (session, pid, or delegations; the goal is followed through
  compression to the chain tip) are prompt and release the conversation's held
  batches. The busy-turn #112033 chat receipt is sent at once, independent of the
  hold. Batches are partitioned by `parent_session_id` so nothing mixes across
  `/new`. The window is read per owning profile. Shutdown releases held batches for
  delivery; while draining, injection goes to the drain spool, a process batch that
  fails or is cancelled while stopping is spooled per event, and delegations that
  cannot finish are requeued (their ledger row stays pending). Regression: `uv run --frozen --group dev pytest -q
  tests/gateway/test_autonomous_wake_pacing.py tests/hermes_cli/test_goals.py
  tests/gateway/test_completion_delivery.py`. Rollback reverts this identity; the
  new `last_continuation_at` field defaults to 0 on old rows and needs no migration.

The initial reproduction established missing criteria and paused state after a
repair turn. Upstream comparison confirmed both and supplied a matching recovery
implementation. Configuration or plugin changes cannot repair these native judge
and admission boundaries. No new tool, permission, or provider is introduced.

Any admitted real user turn can revive a blocker pause, including a question about
the blocker. This adopts upstream's conversational recovery policy, not semantic
proof that the blocker is solved. The next judge may block again. Explicit user
pauses remain authoritative.

- Fork patch identity: `goal-user-authority`. Own fork patch, no upstream PR yet.
  The judge sees what the user actually typed since the goal was set, in an
  authoritative block that keeps each reply-quote header for context (newest 20,
  8,000 characters). When a relay from another session in the same profile reaches
  the goal session, the typed messages of that sender session are included and
  attributed, so a cross-session approval counts only through its original and
  only messages typed before that delivery are imported. Packing keeps the newest
  messages and stops at the first one that does not fit, marking it omitted rather
  than excerpting it, so a newer refusal can never be dropped while an older
  approval stays. Relay
  deliveries, identified by the trailing `[relay from=… receipt=…]` header, are
  never user-typed, so they cannot satisfy a revision `user_quote`. An explicit
  `/goal resume` over a judge block records a user-command revision that renders
  as user authority with a waiver rule in every judge prompt; the judge does not
  re-block on that reason but still blocks new breaches. An agent revision cannot
  imitate it. Reviving on an
  ordinary reply records no override, since the reply may refuse. Goals with no
  typed user input render a byte-identical prompt. Regression:
  `scripts/run_tests.sh tests/hermes_cli/test_goal_user_authority.py
  tests/hermes_cli/test_goals.py`. Retire when upstream judges see user input and
  exclude relays; rollback reverts this identity's block, relay check, resume
  revision and tests.

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
