"""Delivery-time observation for native mid-turn steers, never queued input."""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


def _requeue_pending_steer(agent, steer_text: str) -> None:
    """Put drained steer text back so the caller's fallback delivers it as a next-turn user message."""
    # Under the lock the slot is read directly: an initialized agent always has both attributes, so a
    # missing ``_pending_steer`` there is a real bug and must fail loud. The lock-less branch only
    # exists for test stubs built via ``object.__new__`` that skipped ``__init__``.
    _lock = getattr(agent, "_pending_steer_lock", None)
    if _lock is not None:
        with _lock:
            if agent._pending_steer:
                agent._pending_steer = agent._pending_steer + "\n" + steer_text
            else:
                agent._pending_steer = steer_text
    else:
        existing = getattr(agent, "_pending_steer", None)
        agent._pending_steer = (existing + "\n" + steer_text) if existing else steer_text


def report_steer_delivered(agent, text: str) -> None:
    """Report a completed row insertion once; observer failures cannot undo delivery."""
    ordinal = getattr(agent, "_steer_delivery_ordinal", 0) + 1
    agent._steer_delivery_ordinal = ordinal
    delivery_id = str(uuid.uuid4())
    try:
        from hermes_cli.lifecycle import invoke_hook
        from agent.steer_deferral import auto_messages_for_delivery, manual_text_for_delivery
        invoke_hook(
            "post_steer_delivered",
            session_id=getattr(agent, "session_id", None),
            turn_id=getattr(agent, "_current_turn_id", ""),
            platform=getattr(agent, "platform", ""),
            text=text, delivery_id=delivery_id, ordinal=ordinal,
            manual_text=manual_text_for_delivery(agent, text),
            **({"auto_messages": auto_messages} if (auto_messages := auto_messages_for_delivery(agent, text)) else {}),
        )
    except Exception:
        logger.warning("post_steer_delivered hook failed", exc_info=True)
