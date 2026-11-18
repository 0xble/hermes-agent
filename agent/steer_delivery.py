"""Delivery-time observation for native mid-turn steers, never queued input."""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


def _requeue_pending_steer(agent, steer_text: str) -> None:
    """Put drained steer text back so the caller's fallback delivers it as a next-turn user message."""
    from agent.interrupt_control import requeue_drained_steer
    requeue_drained_steer(agent, steer_text)


def report_steer_delivered(agent, text: str) -> None:
    """Report a completed row insertion once; observer failures cannot undo delivery."""
    from agent.interrupt_control import _ic_lock
    with _ic_lock(agent, "_pending_steer_lock"):
        drained, parts = vars(agent).pop("_steer_last_drain", (None, []))
    if drained == text:
        for part in parts:
            if part.on_delivered is not None:
                part.on_delivered()
    ordinal = getattr(agent, "_steer_delivery_ordinal", 0) + 1
    agent._steer_delivery_ordinal = ordinal
    delivery_id = str(uuid.uuid4())
    try:
        from hermes_cli.lifecycle import invoke_hook
        invoke_hook(
            "post_steer_delivered",
            session_id=getattr(agent, "session_id", None),
            turn_id=getattr(agent, "_current_turn_id", ""),
            platform=getattr(agent, "platform", ""),
            text=text, delivery_id=delivery_id, ordinal=ordinal,
        )
    except Exception:
        logger.warning("post_steer_delivered hook failed", exc_info=True)
