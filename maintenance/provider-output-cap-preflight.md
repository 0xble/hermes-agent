# Provider output-cap preflight

- **Patch identity:** `provider-output-cap-preflight`.
- **Follow-up patch identity:** `session-compression-recovery` (review correction only).
- **Behavior:** Price the assembled provider wire request, including native projections and tool schemas, against the provider's effective output cap. A fresh usage-anchored wire figure fails closed on a proven overflow, including when compression is disabled, exhausted, or attempt-capped; only a real armed checkpoint latch withholds that anchor so a native pruned payload can win. Size a shrink from the same input figure the fit decision used, and never above the currently selected cap. Apply that cap only as turn-scoped state, reusing it for retries and clearing the ceiling, the one-shot ephemeral, and the retry-boost flag on terminal exits and turn start; never undercut Anthropic manual-thinking or Gemini thinking floors, and leave Bedrock's cap semantics unchanged.
- **Source surfaces:** `agent/preflight_output_fit.py`, `agent/chat_completion_helpers.py`, `agent/anthropic_adapter.py`, `agent/transports/chat_completions.py`, `agent/turn_preflight_gate.py`, and focused preflight/provider transport tests.
- **Upstream status:** This fork-specific behavior is part 2 of the #613 split and is not present on the current fork `main` baseline.
- **Focused regression:** `uv run --group dev pytest -q tests/agent/test_native_preflight_estimate.py tests/agent/test_provider_output_cap_preflight.py tests/agent/test_preflight_gate_output_cap.py tests/agent/test_anthropic_adapter.py tests/agent/transports/test_chat_completions.py`.
- **Retirement:** Remove this unit and its row when upstream provides equivalent provider-accurate request-fit and turn-scoped output-cap behavior.
- **Rollback:** Revert the commits carrying `Fork-Patch: provider-output-cap-preflight`.
