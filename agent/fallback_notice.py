"""Notice policy for a bound fallback route and its backend credential surface."""
import contextlib
import logging
import math
import time
from urllib.parse import urlsplit

from hermes_cli.route_identity import normalize_route_base_url

# Keep the fallback walk's logger namespace so existing filters retain these diagnostics.
logger = logging.getLogger("agent.chat_completion_helpers")


def resolved_route(agent) -> tuple[str, str, str]:
    """Resolved model, endpoint and wire; the caller must also compare backend identity."""
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
    """Silence identical alias retries, never distinct first-class credential surfaces."""
    from agent.backend_identity import BackendIdentity, _both_first_class
    from agent.chat_completion_helpers import (
        _buffer_fallback_notice, _fallback_reason_text, _rate_limit_fallback_notice,
    )
    from agent.redact import redact_sensitive_text

    new_route = resolved_route(agent)
    same_route = (all(old_route) and old_route == new_route
                  and not _both_first_class(BackendIdentity.build(provider=old_provider),
                                            BackendIdentity.build(provider=agent.provider)))
    agent._provider_fallback_silent_retry = same_route and (
        not getattr(agent, "_provider_fallback_active", False)
        or getattr(agent, "_provider_fallback_silent_retry", False))
    logger.info("Fallback resolved routes: %s -> %s (silent retry=%s)",
                redact_sensitive_text(str(old_route), force=True, redact_url_credentials=True),
                redact_sensitive_text(str(new_route), force=True, redact_url_credentials=True), same_route)
    # A custom label-only alias is another bounded attempt, not a backend switch.
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


def finish_fallback_restore(agent, was_active, held_shared_record, previous_model, previous_provider):
    """End the episode, retaining silence only for its pending successful-primary receipt."""
    from agent.notice_style import ContextNotice

    silent_retry = bool(getattr(agent, "_provider_fallback_silent_retry", False))
    agent._provider_fallback_active = False
    agent._provider_fallback_silent_retry = False
    agent._provider_fallback_route = None
    record = getattr(agent, "_shared_primary_cooldown_record", None)
    if held_shared_record and isinstance(record, dict):
        # Local receipt metadata, never written to the shared cooldown file. A silent retry
        # still waits for primary success; clearing the episode must not announce its recovery.
        agent._shared_primary_cooldown_record = {**record, "_silent_retry": silent_retry}
    if was_active and not held_shared_record and not silent_retry:
        # Notification surfaces are best-effort and must not undo a successful restore.
        with contextlib.suppress(Exception):
            agent._emit_diagnostic_status(ContextNotice(f"✅ Primary model restored: {agent.model} via {agent.provider}; "
                f"fallback {previous_model} via {previous_provider} is no longer active."))
