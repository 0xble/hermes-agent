"""Mid-turn steer delivery: the insertion receipt plugins observe, and the requeue fallback."""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


def _requeue_pending_steer(agent, steer_text: str) -> None:
    """Put drained steer text back so the caller's fallback delivers it as a next-turn user message."""
    from agent.interrupt_control import requeue_drained_steer
    requeue_drained_steer(agent, steer_text)


def report_steer_delivered(agent, text: str) -> None:
    """Fire ``post_steer_delivered`` once for a steer row just inserted into the turn's messages.

    Called only after the row is in place, so an observer failure can never undo the delivery.
    """
    ordinal = getattr(agent, "_steer_delivery_ordinal", 0) + 1
    agent._steer_delivery_ordinal = ordinal
    try:
        from hermes_cli.lifecycle import invoke_hook
        invoke_hook(
            "post_steer_delivered",
            session_id=getattr(agent, "session_id", None),
            turn_id=getattr(agent, "_current_turn_id", ""),
            platform=getattr(agent, "platform", ""),
            text=text, delivery_id=str(uuid.uuid4()), ordinal=ordinal,
        )
    except Exception:
        logger.warning("post_steer_delivered hook failed", exc_info=True)
