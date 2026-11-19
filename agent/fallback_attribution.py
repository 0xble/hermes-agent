"""Attribute Hermes route transitions without logging credentials or raw provider bodies."""

import logging

logger = logging.getLogger(__name__)


def note_fallback(agent, old_model, model, provider, reason):
    previous = fallback_attribution(agent)
    primary = getattr(agent, "_primary_runtime", None)
    primary_model = primary.get("model") if isinstance(primary, dict) else None
    requested = primary_model or previous.get("requested_model") or old_model
    reason = getattr(reason, "value", reason) or "retry_exhausted"
    agent._fallback_attribution = {
        "requested_model": requested,
        "fallback_model": model,
        "fallback_reason": reason,
    }
    logger.info("Model fallback: requested_model=%s fallback_model=%s provider=%s reason=%s",
                   requested, model, provider, reason)


def fallback_attribution(agent):
    info = getattr(agent, "_fallback_attribution", None)
    return dict(info) if isinstance(info, dict) else {}
