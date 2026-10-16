"""Provider-accurate request fit helpers for the preflight gate.

This is deliberately a small compatibility layer: same-session recovery owns its
future projection API, while this PR only needs the shared wire-fit calculation.
"""

from __future__ import annotations

from typing import Any, Optional

_MIN_SHRUNK_OUTPUT_TOKENS = 4096


def _reserve(window: int) -> int:
    """Estimator/transport framing headroom."""
    return max(1024, int(window * 0.05))


def _request_input_tokens(agent: Any, messages: list, prompt: str, *, wire: bool, tools) -> int:
    effective_prompt = (prompt or "") + ("" if wire else (getattr(agent, "ephemeral_system_prompt", "") or ""))
    request_tools = tools if (wire or tools is not None) else getattr(agent, "tools", None)
    native_prompt = effective_prompt
    if wire and not native_prompt:
        from agent.message_content import flatten_message_text

        native_prompt = "\n".join(
            flatten_message_text(row.get("content"))
            for row in messages
            if isinstance(row, dict) and row.get("role") == "system"
        )
    try:
        from agent.codex_responses_adapter import estimate_native_responses_preflight_tokens

        native = estimate_native_responses_preflight_tokens(
            agent, messages, system_prompt=native_prompt, tools=request_tools,
        )
    except Exception:
        native = None
    if isinstance(native, int) and not isinstance(native, bool) and native >= 0:
        return native
    from agent.model_metadata import estimate_request_tokens_rough

    return estimate_request_tokens_rough(
        messages, system_prompt=effective_prompt, tools=request_tools,
    )


def _native_wire_tokens(agent: Any, messages: list, tools) -> Optional[int]:
    from agent.message_content import flatten_message_text

    prompt = "\n".join(
        flatten_message_text(row.get("content"))
        for row in messages
        if isinstance(row, dict) and row.get("role") == "system"
    )
    try:
        from agent.codex_responses_adapter import estimate_native_responses_preflight_tokens

        native = estimate_native_responses_preflight_tokens(
            agent, messages, system_prompt=prompt, tools=tools,
        )
    except Exception:
        return None
    if isinstance(native, int) and not isinstance(native, bool) and native >= 0:
        return native
    return None


def wire_accurate_input_tokens(
    agent: Any, messages: list, *, tools=None, request_pressure_tokens: Any = None,
) -> Optional[int]:
    """Input size from a provider-grounded source, else None.

    A fresh usage anchor (real ``prompt_tokens`` plus delta) or the checkpoint-pruned
    native payload estimate is accurate enough to fail closed on. The generic
    chars/4 heuristic is not: it may only lower the output cap or wait for real usage.

    A native checkpoint is one exception: the adapter's estimate is the payload the
    next request actually sends, including the current turn. An older usage anchor can
    describe the input that produced the checkpoint rather than its pruned replay, so
    the native figure wins while the one-response deferral latch is a real armed bool.
    An unstubbed mock latch is not that signal.
    """
    native = _native_wire_tokens(agent, messages, tools)
    awaiting = getattr(
        getattr(agent, "context_compressor", None),
        "awaiting_real_usage_after_compression",
        False,
    )
    if native is not None and awaiting is True:
        return native
    if getattr(agent, "_request_pressure_anchored", False) is True and isinstance(
        request_pressure_tokens, int
    ) and not isinstance(request_pressure_tokens, bool) and request_pressure_tokens >= 0:
        return request_pressure_tokens
    return native


def request_fits(
    agent: Any, messages: list, prompt: str, *, wire: bool = False, tools=None,
    input_tokens: Optional[int] = None,
) -> bool:
    """Whether the assembled request plus its effective provider output cap fits."""
    window = getattr(getattr(agent, "context_compressor", None), "context_length", None)
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        return False
    from agent.chat_completion_helpers import effective_wire_max_output_tokens, selected_max_output_tokens

    cap, enforced = effective_wire_max_output_tokens(agent)
    if not enforced:
        # Non-validating endpoints accept a native/default cap larger than the remaining window
        # and stop generation at the window. Reserve only an explicitly selected cap there.
        cap = selected_max_output_tokens(agent)
    output = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else 0
    if input_tokens is None:
        input_tokens = _request_input_tokens(agent, messages, prompt, wire=wire, tools=tools)
    return input_tokens + output + _reserve(window) < window


def affordable_output_cap(
    agent: Any, messages: list, *, tools=None, input_tokens: Optional[int] = None,
) -> Optional[int]:
    """Largest useful output cap that fits beside the assembled wire request.

    ``input_tokens`` is the figure the caller already used to decide the request does not
    fit. Recomputing via the chars/4 estimator can undercount that figure and arm a cap
    that still overflows the anchored measurement. The result is never larger than the
    currently selected cap (configured, provider-clamped, or an already-armed ephemeral),
    so a shrink cannot raise ``max_tokens`` or replace a provider-reported floor with a
    larger local estimate. A selected cap below the useful-runway floor means there is
    nothing to arm.
    """
    window = getattr(getattr(agent, "context_compressor", None), "context_length", None)
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        return None
    if input_tokens is None:
        input_tokens = _request_input_tokens(agent, messages, "", wire=True, tools=tools)
    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 0:
        return None
    room = window - _reserve(window) - input_tokens - 1
    if room < _MIN_SHRUNK_OUTPUT_TOKENS:
        return None
    from agent.chat_completion_helpers import selected_max_output_tokens

    selected = selected_max_output_tokens(agent)
    if isinstance(selected, int) and not isinstance(selected, bool) and selected > 0:
        room = min(room, selected)
    return room if room >= _MIN_SHRUNK_OUTPUT_TOKENS else None
