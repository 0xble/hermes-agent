"""Pre-API pressure gate for the conversation turn loop: the Ollama runtime-context floor,
the provider-overflow re-check arming, the insufficient-progress blocker (compares fully
assembled requests, not raw ``messages``) and the call into
``turn_preflight.run_preflight_compression``. Nothing here imports
``agent.conversation_loop`` at module level (cycle)."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any

from agent.message_metadata import append_message
from agent.turn_context import _compression_warrants_another_preflight_pass
from agent.turn_preflight import PreflightGateVerdict, run_preflight_compression

logger = logging.getLogger("agent.conversation_loop")


def _clear_preflight_output_state(agent: Any) -> None:
    """Do not carry a failed turn's preflight cap into the cached agent's next turn."""
    agent._preflight_output_ceiling = None
    agent._ephemeral_max_output_tokens = None


def run_preflight_gate(
    agent: Any, *, request_pressure_tokens: Any, _moa_prepared_request: Any,
    pending_moa_prepared_request: Any, messages: Any, system_message: Any, user_message: Any,
    active_system_prompt: Any, conversation_history: Any, api_call_count: Any,
    compression_attempts: Any, max_compression_attempts: Any, effective_task_id: Any,
    final_response: Any, failed: Any, _turn_exit_reason: Any, _compression_timeout_exhausted: Any,
    _preflight_compression_blocked: Any, _provider_overflow_recovery_pending: Any,
    _last_preflight_pressure: Any, api_messages: Any = None, tools_for_api: Any = None,
) -> PreflightGateVerdict:
    """Run the pre-API guard chain in the original order. ``_last_preflight_pressure`` is
    consumed here (set to None) and re-armed only by a compression pass, so a blocked
    preflight never compares against a stale figure."""
    from agent.conversation_loop import _ollama_context_limit_error

    v = PreflightGateVerdict(
        action="fallthrough", pending_moa_prepared_request=pending_moa_prepared_request,
        messages=messages, active_system_prompt=active_system_prompt,
        conversation_history=conversation_history, api_call_count=api_call_count,
        compression_attempts=compression_attempts, final_response=final_response, failed=failed,
        _turn_exit_reason=_turn_exit_reason,
        _compression_timeout_exhausted=_compression_timeout_exhausted,
        _preflight_compression_blocked=_preflight_compression_blocked,
        _provider_overflow_recovery_pending=_provider_overflow_recovery_pending,
        _last_preflight_pressure=None,
    )

    _runtime_context_error = _ollama_context_limit_error(agent, request_pressure_tokens)
    if _runtime_context_error:
        v.final_response = _runtime_context_error
        v.failed = True
        v._turn_exit_reason = "ollama_runtime_context_too_small"
        append_message(messages, {"role": "assistant", "content": v.final_response})
        agent._emit_diagnostic_status("❌ Ollama runtime context is too small for Hermes tool use")
        v.api_call_count -= 1
        agent._api_call_count = v.api_call_count
        with suppress(Exception):
            agent.iteration_budget.refund()
        v.action = "break"
        _clear_preflight_output_state(agent)
        return v

    # A known hard-window breach cannot defer to real usage: some servers truncate
    # instead of rejecting. Measure the final request, not a stale usage anchor.
    from agent.compression_recovery import affordable_output_cap, request_fits
    window = getattr(agent.context_compressor, "context_length", None)
    can_shrink_output = getattr(agent, "api_mode", None) not in {"codex_app_server", "bedrock_converse"}
    if (api_messages is not None and isinstance(window, int) and window > 0
            and can_shrink_output
            and not request_fits(agent, api_messages, "", wire=True, tools=tools_for_api)):
        # Input that fits with a useful answer runway only needs a smaller output cap, the
        # same remedy the provider's "max_tokens too large" clamp applies after a rejection.
        shrunk = affordable_output_cap(agent, api_messages, tools=tools_for_api)
        if shrunk is not None:
            if getattr(agent, "api_mode", None) not in {"bedrock_converse", "codex_app_server"}:
                agent._preflight_output_ceiling = shrunk
            agent._ephemeral_max_output_tokens = shrunk
            logger.info("Pre-send fit: lowering this request's output cap to %s tokens", f"{shrunk:,}")
    if (api_messages is not None and isinstance(window, int) and window > 0
            and getattr(agent, "api_mode", None) != "codex_app_server"
            and not request_fits(agent, api_messages, "", wire=True, tools=tools_for_api)):
        from agent.conversation_compression import conversation_history_after_compression
        from agent.turn_context_compaction import _refund_api_call
        v.api_call_count = _refund_api_call(agent, v.api_call_count)
        before = list(messages)
        recovered, prompt = messages, active_system_prompt
        if getattr(agent, "compression_enabled", True):
            try:
                recovered, prompt = agent._compress_context(
                    messages, system_message, task_id=effective_task_id,
                    approx_tokens=request_pressure_tokens,
                )
            except Exception:
                logger.warning("Hard-window recovery failed; retaining the same session", exc_info=True)
        if recovered != before:
            v.messages = recovered
            v.active_system_prompt = prompt
            v.conversation_history = conversation_history_after_compression(agent, recovered)
            if can_shrink_output and not request_fits(agent, recovered, prompt, tools=tools_for_api):
                shrunk = affordable_output_cap(agent, recovered, tools=tools_for_api)
                if shrunk is not None:
                    if getattr(agent, "api_mode", None) not in {"bedrock_converse", "codex_app_server"}:
                        agent._preflight_output_ceiling = shrunk
                    agent._ephemeral_max_output_tokens = shrunk
                    logger.info("Post-recovery fit: lowering this request's output cap to %s tokens", f"{shrunk:,}")
            if request_fits(agent, recovered, prompt, tools=tools_for_api):
                v._preflight_compression_blocked = False
                v._last_preflight_pressure = request_pressure_tokens
                v.action = "continue"
                return v
        v.final_response = (
            "The complete request cannot safely fit the model window. No provider call was sent. "
            "This session, its history and accepted input are retained; retry /compress "
            "when the summary route is available."
        )
        v.failed = True
        v._turn_exit_reason = "context_compression_timeout"
        v._compression_timeout_exhausted = True
        v.action = "break"
        _clear_preflight_output_state(agent)
        return v

    # Pre-API pressure check: tool results grow a turn and last_prompt_tokens lags
    # them. Mirror the turn-prologue guard chain: defer on noisy estimate, skip in
    # failure cooldown, then should_compress().
    _compressor = agent.context_compressor
    _preflight_threshold = int(getattr(_compressor, "threshold_tokens", 0) or 0)
    _provider_overflow_preflight = _provider_overflow_recovery_pending and (
        _preflight_threshold <= 0 or request_pressure_tokens >= _preflight_threshold
    )
    if _provider_overflow_recovery_pending and not _provider_overflow_preflight:
        # The outer-loop rebuild includes system prompt, request-only injections and
        # tool schemas; only that full request with output runway may be sent.
        v._provider_overflow_recovery_pending = False
    # Compare fully assembled requests, not raw ``messages`` (which omit
    # api_content, plugin injections, prefills, MoA context, ephemeral system text).
    if (
        _last_preflight_pressure is not None
        and request_pressure_tokens >= _preflight_threshold
        and not _compression_warrants_another_preflight_pass(
            _last_preflight_pressure, request_pressure_tokens, _preflight_threshold
        )
    ):
        # Stop proactive retries this turn without consuming the shared overflow-
        # recovery budget; the provider's error handler may still compact.
        v._preflight_compression_blocked = True
        logger.warning(
            "Pre-API compression made insufficient progress: ~%s -> "
            "~%s request tokens; skipping additional preflight passes",
            f"{_last_preflight_pressure:,}",
            f"{request_pressure_tokens:,}",
        )
    result = run_preflight_compression(
        agent, v, compressor=_compressor, request_pressure_tokens=request_pressure_tokens,
        provider_overflow_preflight=_provider_overflow_preflight,
        # An anchored figure is real usage + delta: never deferred. Only a whole-context rough
        # estimate waits for the provider's count.
        defer_preflight=(
            (lambda _t: False) if getattr(agent, "_request_pressure_anchored", False)
            else getattr(_compressor, "should_defer_preflight_to_real_usage", lambda _t: False)
        ),
        moa_prepared_request=_moa_prepared_request, system_message=system_message,
        user_message=user_message, max_compression_attempts=max_compression_attempts,
        effective_task_id=effective_task_id,
    )
    if result.action == "break":
        _clear_preflight_output_state(agent)
    return result
