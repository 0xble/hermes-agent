# Loop-cap final answer

Identity: `loop-cap-final-answer`

## Required behavior

Only `loop_web_search_cap` and `loop_subagent_cap` leave the controlled-halt path for one bounded final answer from results already collected. Excess calls stay refused with matched synthetic results. Tools returned by that completion are never executed. A failed or tool-only completion uses limit-specific fallback copy and does not loop. Unrelated guardrails, including the unattended continuation limit, stay on their existing path.

Chat-display delivery tracking follows only `stream_delta_callback`. A successful audio/TTS callback must not make a rejected display delivery look complete, and must not suppress the display replay of a cap answer. Prior tool-round commentary is not evidence that the cap completion reached display. Partial display delivery still receives the complete answer. Held streams stay held.

## Proof surface

`tests/agent/test_tool_guardrails.py` covers refusal without advancing the executed count. `tests/agent/test_tool_call_guardrail_runtime.py` covers both caps, exhausted and open iteration budgets, one completion, and discarded follow-up tool calls. `tests/agent/test_stream_delivery_display_tts.py` covers display rejection plus TTS success, display success plus TTS rejection, and paired success. Existing iteration-summary tests cover the non-cap completion.

## Upstream disposition and retirement

Retire this record when released upstream gives both per-turn caps one bounded answer from collected results, never executes tools from that answer, and tracks chat display independently of audio delivery. Rollback is the commit carrying `Fork-Patch: loop-cap-final-answer`.
