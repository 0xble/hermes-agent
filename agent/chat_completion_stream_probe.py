"""Eligibility and throttling window for streaming 5xx unmask probes."""
from typing import Any

from agent.error_classifier import FailoverReason, _extract_status_code, classify_api_error
from agent.fallback_cooldown import _RATE_LIMIT_FAILOVER_REASONS

# One non-streaming re-issue across outer retries. Later turns re-arm automatically.
_STREAM_5XX_PROBE_WINDOW_S = 60.0


def stream_error_allows_unmask(agent: Any, error: Exception, deltas_were_sent: bool) -> bool:
    """Probe only opaque pre-delivery chat errors, never a definitive shared outage."""
    from agent.chat_completion_helpers import _streams_llamacpp

    if getattr(agent, "_interrupt_requested", False) or _streams_llamacpp(agent):
        return False  # the local router cannot cancel this probe after its watchdog gives up
    status = _extract_status_code(error)
    if status is None or status < 500 or deltas_were_sent:
        return False
    if getattr(agent, "api_mode", "") not in ("", "chat_completions"):
        return False  # replay handles chat-completions shapes only
    classified = classify_api_error(
        error, provider=getattr(agent, "provider", "") or "", model=getattr(agent, "model", "") or "",
        base_url=str(getattr(agent, "base_url", "") or ""), api_key=getattr(agent, "api_key", None),
    )
    # A bare 503 is also classified as overloaded. Only an explicit provider-wide verdict
    # makes the probe redundant. Keep opaque 503s eligible to reveal hidden validation errors.
    return not (classified.reason in _RATE_LIMIT_FAILOVER_REASONS or (
        classified.reason == FailoverReason.overloaded and classified.error_context.get("provider_wide")
    ))
