"""Bounded fast-mode windows (``/fast auto`` and ``/fast cold``).

``agent.service_tier``: ``None`` (normal), ``"priority"`` / ``"ultrafast"`` (static tiers,
pinned into ``agent.request_overrides`` at build time), ``"auto"`` (every user turn opens a
window of ``agent.fast_auto_seconds``) or ``"cold"`` (only a session's first turn,
no prior history, opens it). The provider's fast override is layered onto request
kwargs only while the window is open; only per-request params (``service_tier`` /
``speed``) vary, so the request body stays byte-identical. Anthropic keeps a separate
prompt cache per speed, so each Anthropic window boundary re-writes the prefix at the
new speed.
"""

from __future__ import annotations

import time
from typing import Any

BOUNDED_MODES = frozenset({"auto", "cold"})
DEFAULT_WINDOW_SECONDS = 60
# Documented fast-mode rate-limit headers; a limit of 0 means the organization has no fast
# capacity for the model (https://platform.claude.com/docs/en/build-with-claude/fast-mode).
_FAST_LIMIT_HEADERS = ("anthropic-fast-input-tokens-limit", "anthropic-fast-output-tokens-limit")
#: Tiers sent on every request of the session (OpenAI ``service_tier`` values; ``priority`` also
#: selects Anthropic/xAI fast mode). Ultrafast is OpenAI-only and gated per model.
STATIC_TIERS = frozenset({"priority", "ultrafast"})
NORMAL_TIER_WORDS = frozenset({"", "normal", "default", "standard", "off", "none"})
# User/config word -> agent.service_tier. The single table every surface (config loaders, /fast
# on CLI / gateway / TUI) parses through, so a new tier is one edit.
SERVICE_TIER_WORDS: dict[str, str] = {
    "fast": "priority", "priority": "priority", "on": "priority",
    "ultrafast": "ultrafast", "auto": "auto", "cold": "cold",
}


def parse_service_tier(raw: Any) -> str | None:
    """``agent.service_tier`` for a user/config word; None for normal and for unknown words."""
    value = str(raw or "").strip().lower()
    return None if value in NORMAL_TIER_WORDS else SERVICE_TIER_WORDS.get(value)


def service_tier_word(tier: Any) -> str:
    """The user-facing word for a stored tier (``priority`` -> ``fast``, None/"" -> ``normal``)."""
    return {"priority": "fast", None: "normal", "": "normal"}.get(tier, tier)


def begin_turn(agent: Any, conversation_history: Any) -> None:
    """Open (or refuse) the fast window at a user-turn boundary."""
    mode = getattr(agent, "service_tier", None)
    agent._fast_until = 0.0
    if mode not in BOUNDED_MODES:
        return
    if mode == "cold" and any(
        isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")
        for m in (conversation_history or ())
    ):
        return
    try:
        window = float(getattr(agent, "fast_auto_seconds", DEFAULT_WINDOW_SECONDS))
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW_SECONDS
    agent._fast_until = time.monotonic() + max(window, 0.0)


def format_expiry_duration(seconds: Any) -> str:
    """Format the configured duration for the expiry notice."""
    try:
        value = float(seconds)
        if not value > 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return "1s"
    if value < 60:
        return f"{max(1, int(round(value)))}s"
    total_minutes = max(1, int(round(value / 60)))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def format_expiry_remaining(seconds: Any) -> str:
    """Format remaining time as compact hours/minutes for status and picker text."""
    try:
        total_minutes = max(1, int(float(seconds) / 60))
    except (TypeError, ValueError, OverflowError):
        total_minutes = 1
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _gateway_fast_overlay(agent: Any, tier: str | None) -> dict[str, Any]:
    """Resolve the Fast-owned wire overlay for one gateway agent and static tier."""
    if tier not in STATIC_TIERS:
        return {}
    from hermes_cli.models import resolve_fast_mode_overrides
    base_url = getattr(agent, "base_url", None)
    if getattr(agent, "api_mode", None) == "anthropic_messages":
        base_url = getattr(agent, "_anthropic_base_url", None) or base_url
    return dict(resolve_fast_mode_overrides(
        getattr(agent, "model", None), provider=getattr(agent, "provider", None),
        base_url=base_url, tier=tier,
    ) or {})


def set_gateway_fast_expiry_state(
    agent: Any, expiry_at: Any, tier: str | None, overlay: dict[str, Any] | None = None,
) -> None:
    """Record gateway expiry metadata used by the non-mutating wire-time filter.

    ``overlay`` is the exact Fast mapping the caller added; without it the overlay is resolved
    from the agent's route the same way the turn route resolves it."""
    agent._gateway_fast_expiry_at = (
        expiry_at if isinstance(expiry_at, (int, float)) and not isinstance(expiry_at, bool) else 0.0
    )
    agent._gateway_session_fast_overlay = (
        dict(overlay) if isinstance(overlay, dict) else _gateway_fast_overlay(agent, tier)
    )


def effective_request_overrides(agent: Any) -> dict[str, Any]:
    """Read request overrides, dropping only an expired session Fast overlay."""
    overrides = dict(getattr(agent, "request_overrides", None) or {})
    expiry_at = getattr(agent, "_gateway_fast_expiry_at", 0.0)
    overlay = getattr(agent, "_gateway_session_fast_overlay", {})
    if (
        isinstance(expiry_at, (int, float)) and not isinstance(expiry_at, bool) and expiry_at > 0
        and time.time() >= expiry_at and isinstance(overlay, dict)
    ):
        for key, value in overlay.items():
            if overrides.get(key) == value:
                overrides.pop(key, None)
    if getattr(agent, "service_tier", None) in BOUNDED_MODES and time.monotonic() < getattr(agent, "_fast_until", 0.0):
        from hermes_cli.models import resolve_fast_mode_overrides
        base_url = getattr(agent, "base_url", None)
        if getattr(agent, "api_mode", None) == "anthropic_messages":
            base_url = getattr(agent, "_anthropic_base_url", None) or base_url
        overrides.update(
            resolve_fast_mode_overrides(getattr(agent, "model", None), provider=getattr(agent, "provider", None), base_url=base_url) or {}
        )
    if "speed" in overrides and getattr(agent, "model", None) in (getattr(agent, "_fast_mode_unavailable_models", None) or ()):
        overrides.pop("speed", None)
    return overrides


def fast_mode_unprovisioned(api_error: Any, api_kwargs: Any) -> bool:
    """True for a 429 on a ``speed: "fast"`` request that can never succeed at fast speed: the
    fast-mode limit header is 0 (no fast capacity for the model), or the body is Anthropic's
    usage-credit refusal (the account has no credits for fast mode). Proxies may drop the limit
    headers but relay the body, so both are checked. Waiting or rotating keys cannot help."""
    if getattr(api_error, "status_code", None) != 429 or not isinstance(api_kwargs, dict):
        return False
    if (api_kwargs.get("extra_body") or {}).get("speed") != "fast":
        return False
    if _fast_mode_credit_refusal(api_error):
        return True
    headers = getattr(getattr(api_error, "response", None), "headers", None)
    if headers is None:
        return False
    return any(str(headers.get(name, "")).strip() == "0" for name in _FAST_LIMIT_HEADERS)


def _fast_mode_credit_refusal(api_error: Any) -> bool:
    """Anthropic's "Usage credits are required for fast mode." A genuine rate limit never
    mentions fast mode, so this cannot swallow one."""
    body = getattr(api_error, "body", None)
    message = ((body.get("error") or {}).get("message") if isinstance(body, dict) else None) or str(api_error)
    message = str(message).lower()
    return "fast mode" in message and ("usage credits" in message or "credits are required" in message)


def mark_fast_mode_unavailable(agent: Any) -> bool:
    """Stop sending ``speed`` for the current model for the rest of the session. False when the
    model was already marked, so the caller retries at most once per model."""
    model = getattr(agent, "model", None)
    unavailable = getattr(agent, "_fast_mode_unavailable_models", None)
    if not isinstance(unavailable, set):
        unavailable = agent._fast_mode_unavailable_models = set()
    if not model or model in unavailable:
        return False
    unavailable.add(model)
    return True
