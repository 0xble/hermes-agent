# Loop-cap final answer

**Patch identity:** `loop-cap-final-answer`.

## Divergence and adoption

Upstream cap commits `b68787ad25`, `cb06017b1d`, and `226c765709`
added bounded search/delegation refusal. At upstream `main`
`26c8119e3480825c827fce78e35a95a4169c32dba`, cap decisions still cause
`run_tool_round` to end with the generic controlled-halt copy. Upstream
issues [#85746](https://github.com/NousResearch/hermes-agent/issues/85746)
and [#102694](https://github.com/NousResearch/hermes-agent/issues/102694)
report premature cap stops; neither has a merged fix at inspection.

Contribution decision: upstream candidate; fork delivery is this narrow
adaptation, not adoption of moving upstream `main`.

## Preservation and proof

Only `loop_web_search_cap` and `loop_subagent_cap` take the existing
`handle_max_iterations` final-answer path. Excess calls retain matched synthetic
results. The cap path attempts one completion, never dispatches any returned
tool calls, and uses truthful limit-specific fallback copy if it cannot answer.
Identical-call and repeated-failure halts retain their existing path.

Proof owners: `tests/agent/test_tool_guardrails.py`,
`tests/agent/test_tool_call_guardrail_runtime.py`, and the existing iteration
summary tests in `tests/agent/test_run_agent.py`. Preserve result visibility,
bounded completion behavior, strict request-prefix handling, streamed final
text, interruption, and iteration-budget summary behavior when upstream changes
these seams.

## Retirement and rollback

Retire the adaptation only when released upstream lets both per-turn caps
produce a bounded final answer from prior results without executing more calls.
Rollback: revert the commit carrying `Fork-Patch: loop-cap-final-answer`.
