"""Notice policy for a bound fallback route and its backend credential surface."""
import contextlib
import logging
import math
import time
from urllib.parse import urlsplit

from hermes_cli.route_identity import normalize_route_base_url
from agent.error_classifier import FailoverReason

logger = logging.getLogger("agent.chat_completion_helpers")

_FALLBACK_REASON_LABELS = {reason: reason.value.replace("_", " ") for reason in FailoverReason}
_FALLBACK_DETAIL_MAX_CHARS = 200


def fallback_reason_text(reason):
    return _FALLBACK_REASON_LABELS.get(reason) or str(getattr(reason, "value", None) or reason or "provider failure").replace("_", " ")


def fallback_trigger_text(agent, reason):
    label = fallback_reason_text(reason)
    stashed = getattr(agent, "_fallback_trigger", None)
    agent._fallback_trigger = None
    if not (isinstance(stashed, tuple) and len(stashed) == 2 and reason is not None and stashed[0] == reason):
        return label
    from agent.redact import redact_sensitive_text
    detail = " ".join(redact_sensitive_text(str(stashed[1] or "")).split())
    if not detail:
        return label
    if len(detail) > _FALLBACK_DETAIL_MAX_CHARS:
        detail = detail[:_FALLBACK_DETAIL_MAX_CHARS - 1].rstrip() + "…"
    return f"{label}: {detail}"


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
    """End the episode, retaining silence only for its pending successful-primary receipt."""
    from agent.notice_style import ContextNotice
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
        # Notification surfaces are best-effort and must not undo a successful restore.
        with contextlib.suppress(Exception):
            agent._emit_diagnostic_status(ContextNotice(f"✅ Primary model restored: {agent.model} via {agent.provider}; "
                f"fallback {previous_model} via {previous_provider} is no longer active."))


def record_fallback_trigger(agent, reason, error_text: str) -> None:
    """Remember a classified provider error for the next fallback notice."""
    from agent.credential_pool_errors import redact_error_text
    known = (getattr(agent, "api_key", None), getattr(agent, "_anthropic_api_key", None))
    agent._fallback_trigger = (reason, redact_error_text(str(error_text or ""), known))


def fallback_notice_hidden(agent) -> bool:
    """A background wake turn shows no switch notice until a user-visible turn."""
    from gateway.response_filters import is_machinery_display_kind
    return is_machinery_display_kind(getattr(agent, "_turn_display_kind", None))


def surface_hidden_fallback(agent) -> None:
    """Reconcile a switch hidden by a wake turn before the next visible turn runs."""
    held = getattr(agent, "_hidden_fallback_notice", None)
    if not held or fallback_notice_hidden(agent):
        return
    agent._hidden_fallback_notice = None
    if getattr(agent, "_provider_fallback_active", False):
        announce_fallback_switch(agent, held)


def announce_primary_restored(agent, previous_model: str, previous_provider: str) -> None:
    unseen = getattr(agent, "_hidden_fallback_notice", None)
    agent._hidden_fallback_notice = None
    if unseen or fallback_notice_hidden(agent):
        return
    try:
        agent._emit_diagnostic_status(
            f"✅ Primary model restored: {agent.model} via {agent.provider}; "
            f"fallback {previous_model} via {previous_provider} is no longer active.")
    except Exception:
        logger.debug("primary restored notice failed to emit", exc_info=True)


FALLBACK_STATUS_KIND = "fallback"


def announce_fallback_switch(agent, notice: str) -> None:
    """Show a fallback switch before the fallback model answers, or hold it for a wake turn."""
    if fallback_notice_hidden(agent):
        logger.info("fallback switch notice not shown on a background wake turn: %s", notice)
        agent._hidden_fallback_notice = notice
        return
    agent._hidden_fallback_notice = None
    from gateway.warning_notifications import DiagnosticText
    try:
        agent._emit_status_kind(FALLBACK_STATUS_KIND, DiagnosticText(notice), origin="fallback_switch")
    except Exception:
        logger.debug("fallback switch notice failed to emit", exc_info=True)
