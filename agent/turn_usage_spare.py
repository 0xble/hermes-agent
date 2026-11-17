"""Spare-fallback usage accounting for response turns."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("agent.conversation_loop")


def record_spare_fallback_usage(agent: Any, usage: Any) -> None:
    """Record canonical usage for an active spare fallback, without affecting primary usage."""
    try:
        from agent.spare_guards import record_fallback_usage
        record_fallback_usage(agent, usage)
    except Exception:
        logger.debug("Spare fallback usage accounting failed", exc_info=True)
