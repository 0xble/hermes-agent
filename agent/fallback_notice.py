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
    from agent.backend_identity import BackendIdentity, distinct_credential_surface
    from agent.chat_completion_helpers import (
        _buffer_fallback_notice, _fallback_reason_text, _rate_limit_fallback_notice,
    )
    from agent.redact import redact_sensitive_text
    from agent.fallback_route_recovery_uncooled import note_fallback_activated
    from agent.turn_failure_routes import note_route_left

    # Reached only once a fallback is bound, so both record a switch that really happened.
    note_route_left(agent, old_provider, old_model)
    note_fallback_activated(agent)
    new_route = resolved_route(agent)
    same_route = (all(old_route) and old_route == new_route
                  and not distinct_credential_surface(BackendIdentity.build(provider=old_provider),
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
    """End the episode and defer recovery notice until the primary actually succeeds."""
    from agent.turn_failure_routes import reset_routes_left

    reset_routes_left(agent)
    silent_retry = bool(getattr(agent, "_provider_fallback_silent_retry", False))
    try:
        from agent.spare_guards import release_fallback_route
        release_fallback_route(agent)
    except Exception:
        logger.debug("Spare lease release during primary restore failed", exc_info=True)
    agent._provider_fallback_active = False
    agent._provider_fallback_silent_retry = False
    agent._provider_fallback_route = None
    record = getattr(agent, "_shared_primary_cooldown_record", None)
    if held_shared_record and isinstance(record, dict):
        # Local receipt metadata, never written to the shared cooldown file. A silent retry
        # still waits for primary success; clearing the episode must not announce its recovery.
        agent._shared_primary_cooldown_record = {**record, "_silent_retry": silent_retry}
    if was_active and not held_shared_record and not silent_retry:
        # Rebinding at turn start only makes the primary eligible. The next provider response
        # must prove recovery before telling the user, otherwise a provider that fails again
        # emits a false restore notice on every prompt.
        agent._pending_primary_restore_notice = {
            "model": str(agent.model),
            "provider": str(agent.provider),
            "previous_model": str(previous_model),
            "previous_provider": str(previous_provider),
        }
