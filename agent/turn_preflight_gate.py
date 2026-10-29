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
    agent._ephemeral_output_cap_is_retry_boost = False


def _retry_boost_is_active(agent: Any) -> bool:
    """A truncation continuation must stay larger than the previous request.

    ``_arm_output_ceiling`` writes both the durable ceiling and the one-shot cap.
    Doing that while a retry boost is armed would make the ceiling helper ignore
    the boost and send the same undersized request again.
    """
    return bool(getattr(agent, "_ephemeral_output_cap_is_retry_boost", False)) and getattr(
        agent, "_ephemeral_max_output_tokens", None
    ) is not None


def _arm_output_ceiling(agent: Any, shrunk: int) -> bool:
    """Arm a turn output ceiling only when the builder would honor it.

    Provider protocol floors (Anthropic manual-thinking ``budget_tokens + 4096``, Gemini's
    thinking raise) win over the ceiling, so a route whose floor sits above ``shrunk`` gets no
    ceiling at all; the caller's fit check then fails and the recover-or-withhold path runs.
    """
    previous = (getattr(agent, "_preflight_output_ceiling", None),
                getattr(agent, "_ephemeral_max_output_tokens", None))
    if getattr(agent, "api_mode", None) not in {"bedrock_converse", "codex_app_server"}:
        agent._preflight_output_ceiling = shrunk
    agent._ephemeral_max_output_tokens = shrunk
    from agent.chat_completion_helpers import effective_wire_max_output_tokens
    cap, _enforced = effective_wire_max_output_tokens(agent)
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > shrunk:
        agent._preflight_output_ceiling, agent._ephemeral_max_output_tokens = previous
        logger.info("Pre-send fit: route output floor %s exceeds affordable cap %s; not lowering",
                    f"{cap:,}", f"{shrunk:,}")
        return False
    return True


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
        _preflight_api_call_refunded=False,
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
    # instead of rejecting. A fresh usage anchor is that measurement for chat and
    # Anthropic; the native estimator only covers checkpoint-pruned Codex payloads.
    from agent.preflight_output_fit import affordable_output_cap, request_fits, wire_accurate_input_tokens
    _compressor = agent.context_compressor
    window = getattr(_compressor, "context_length", None)
    can_shrink_output = getattr(agent, "api_mode", None) not in {"codex_app_server", "bedrock_converse"}
    # The chars/4 pressure figure is not a hard-window measurement. Only an
    # anchored or checkpoint-pruned native estimate may fail the turn closed.
    _wire_input_tokens = (
        wire_accurate_input_tokens(
            agent, api_messages, tools=tools_for_api,
            request_pressure_tokens=request_pressure_tokens,
        )
        if api_messages is not None else None
    )
    # A fresh usage anchor is provider-grounded and accurate enough to fail closed
    # on, including when compression is disabled, exhausted, or attempt-capped.
    # Only a real armed checkpoint latch withholds that anchor so the native
    # pruned payload can win. An unstubbed mock latch is not a deferral signal.
    _awaiting_checkpoint_usage = (
        getattr(_compressor, "awaiting_real_usage_after_compression", False) is True
    )
    _native_wire_tokens = _wire_input_tokens
    if (api_messages is not None and isinstance(window, int) and window > 0
            and can_shrink_output
            and _wire_input_tokens is not None
            and not _retry_boost_is_active(agent)
            and not request_fits(
                agent, api_messages, "", wire=True, tools=tools_for_api,
                input_tokens=_wire_input_tokens,
            )):
        # Input that fits with a useful answer runway only needs a smaller output cap, the
        # same remedy the provider's "max_tokens too large" clamp applies after a rejection.
        shrunk = affordable_output_cap(
            agent, api_messages, tools=tools_for_api, input_tokens=_wire_input_tokens,
        )
        if shrunk is not None and _arm_output_ceiling(agent, shrunk):
            logger.info("Pre-send fit: lowering this request's output cap to %s tokens", f"{shrunk:,}")
    _preflight_threshold = int(getattr(_compressor, "threshold_tokens", 0) or 0)
    _request_exceeds_window = (
        api_messages is not None and _native_wire_tokens is not None
        and isinstance(window, int) and window > 0
        and not request_fits(
            agent, api_messages, "", wire=True, tools=tools_for_api,
            input_tokens=_native_wire_tokens,
        )
    )
    # Once the provider-grounded figure has been measured again after any attempted
    # output-cap shrink, a remaining fit rejection is a proven wire overflow. The
    # shared request_fits() check already prices the framing reserve and provider
    # floors; do not replace that decision with a raw five-percent input heuristic.
    # Native checkpoint estimates are equally authoritative when the latch is armed:
    # wire_accurate_input_tokens() has already replaced any stale usage anchor with
    # the pruned payload figure, so a rejected native request must not fall through.
    _hard_window_preflight = (
        _request_exceeds_window
        and getattr(agent, "api_mode", None) != "codex_app_server"
    )
    if _hard_window_preflight:
        # Route the proven wire overflow through the same guarded compression path as
        # ordinary preflight: attempt caps, cooldown/lock defers, timeout handling,
        # retry-state reset, MoA cleanup and reference-handoff checks all apply here.
        result = run_preflight_compression(
            agent, v, compressor=_compressor, request_pressure_tokens=request_pressure_tokens,
            provider_overflow_preflight=True, hard_window_preflight=True,
            defer_preflight=lambda _t: False, moa_prepared_request=_moa_prepared_request,
            system_message=system_message, user_message=user_message,
            max_compression_attempts=max_compression_attempts, effective_task_id=effective_task_id,
        )
        if result.action in {"break", "return"} and not _retry_boost_is_active(agent):
            _clear_preflight_output_state(agent)
        return result

    # Pre-API pressure check: tool results grow a turn and last_prompt_tokens lags
    # them. Mirror the turn-prologue guard chain: defer on noisy estimate, skip in
    # failure cooldown, then should_compress().
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
            (lambda _t: True)
            if _awaiting_checkpoint_usage and not getattr(agent, "_request_pressure_anchored", False)
            else (lambda _t: False) if getattr(agent, "_request_pressure_anchored", False)
            else getattr(_compressor, "should_defer_preflight_to_real_usage", lambda _t: False)
        ),
        moa_prepared_request=_moa_prepared_request, system_message=system_message,
        user_message=user_message, max_compression_attempts=max_compression_attempts,
        effective_task_id=effective_task_id,
    )
    if result.action in {"break", "return"} and not _retry_boost_is_active(agent):
        _clear_preflight_output_state(agent)
    return result
