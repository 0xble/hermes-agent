"""Notice policy for a bound fallback route, independent of provider aliases."""
import logging
import math
import time
from urllib.parse import urlsplit

from hermes_cli.route_identity import normalize_route_base_url

logger = logging.getLogger("agent.chat_completion_helpers")


def resolved_route(agent) -> tuple[str, str, str]:
    """Runtime route identity, excluding credentials and display/provider labels."""
    endpoint = normalize_route_base_url(str(getattr(agent, "base_url", "") or ""))
    try:
        parsed = urlsplit(endpoint)
        if "@" in parsed.netloc:
            endpoint = endpoint.replace(parsed.netloc, parsed.netloc.rsplit("@", 1)[-1], 1)
    except ValueError:
        pass  # An unparseable endpoint must not manufacture equivalence.
    return (
        str(getattr(agent, "model", "") or ""),
        endpoint,
        str(getattr(agent, "api_mode", "") or ""),
    )


def buffer_fallback_route_notice(agent, reason, shared_adoption, old_model, old_provider, old_route, cooldown_seconds):
    """Retain switch notices, but treat identical resolved routes as silent retries."""
    from agent.chat_completion_helpers import (
        _buffer_fallback_notice, _fallback_reason_text, _rate_limit_fallback_notice,
    )
    from agent.redact import redact_sensitive_text

    new_route = resolved_route(agent)
    same_route = all(old_route) and old_route == new_route
    agent._provider_fallback_silent_retry = same_route and (
        not getattr(agent, "_provider_fallback_active", False)
        or getattr(agent, "_provider_fallback_silent_retry", False))
    logger.info("Fallback resolved routes: %s -> %s (silent retry=%s)",
                redact_sensitive_text(str(old_route), force=True, redact_url_credentials=True),
                redact_sensitive_text(str(new_route), force=True, redact_url_credentials=True), same_route)
    # A label-only alias is another bounded attempt at the same route, not a model switch.
    if same_route:
        return
    fb_model, fb_provider = agent.model, agent.provider
    use_generic, notice = _rate_limit_fallback_notice(
        agent, reason, shared_adoption, old_model, fb_model, fb_provider)
    if use_generic:
        notice = (
            f"⚠️ Model fallback: {old_model} via {old_provider} unavailable "
            f"({_fallback_reason_text(reason)}); using {fb_model} via {fb_provider}.")
        if cooldown_seconds is not None:
            remaining = max(0, math.ceil(agent._rate_limited_until - time.monotonic()))
            notice += f" Primary retry eligible in ~{remaining} s; recovery is not guaranteed."
    if notice:
        _buffer_fallback_notice(agent, notice)
