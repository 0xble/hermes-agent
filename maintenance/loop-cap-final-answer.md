# Loop-cap final answer

**Patch identity:** `loop-cap-final-answer`.

## Divergence and adoption

Upstream cap commits `b68787ad25`, `cb06017b1d`, and `226c765709`
added bounded search/delegation refusal. At upstream `main`
`41cd7b649d0980c57620035af736f91be10dae79`, cap decisions still cause
`run_tool_round` to end with the generic controlled-halt copy. Related upstream
issues [#85746](https://github.com/NousResearch/hermes-agent/issues/85746)
and [#102694](https://github.com/NousResearch/hermes-agent/issues/102694)
concern inaccurate cap explanations and delegated halt classification, respectively.
Neither issue is evidence of a bounded final-answer implementation.

Contribution decision: upstream candidate; fork delivery is this narrow
adaptation, not adoption of moving upstream `main`.

## Preservation and proof

Only `loop_web_search_cap` and `loop_subagent_cap` take the existing
`handle_max_iterations` final-answer path. Excess calls retain matched synthetic
results. The cap path attempts one completion, never dispatches any returned
tool calls, and uses truthful limit-specific fallback copy if it cannot answer.
Identical-call and repeated-failure halts retain their existing path.

The cap completion resets existing per-response delivery tracking because it
bypasses `build_api_request`. Whole-answer emission is skipped only when the
shared exact-match predicate confirms full delivery of that response. Prior
commentary cannot suppress an answer, and partial or failed streaming retains
full-text replay. Nonstreaming answers and empty/tool-only fallback copy still
reach the display callback. The end-of-stream marker remains emitted. True
controlled halts retain unconditional SSE delivery
([#30770](https://github.com/NousResearch/hermes-agent/issues/30770)), and partial
prefixes do not count as complete delivery
([#88954](https://github.com/NousResearch/hermes-agent/issues/88954)).

The older Io lineage lacks `_interim_content_fully_streamed`. Its backport
adds only that exact comparison to the existing `StreamDeliveryMixin`, reusing
its normalization and successfully delivered text. It adds no provider-specific
condition or parallel delivery state.

Proof owners: `tests/agent/test_tool_guardrails.py`,
`tests/agent/test_tool_call_guardrail_runtime.py`, and the existing iteration
summary tests in `tests/agent/test_run_agent.py`. Preserve result visibility,
bounded completion behavior, strict request-prefix handling, streamed final
text, interruption, and iteration-budget summary behavior when upstream changes
these seams. The cap-stream regression drives `run_conversation` through the
real interruptible Codex request and event consumer using fixture SDK events.
It covers full delivery, terminal-only text after identical prior commentary,
a partial prefix, a rejected display delta, and empty/tool-only fallback.
Existing Chat Completions cap tests cover nonstreaming answers and both cap types.

## Retirement and rollback

Retire the adaptation only when released upstream lets both per-turn caps
produce a bounded final answer from prior results without executing more calls.
Rollback: revert the commit carrying `Fork-Patch: loop-cap-final-answer`.
