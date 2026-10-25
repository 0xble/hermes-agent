"""Shared eligibility state for automatic background skill reviews."""

from __future__ import annotations

import logging
from typing import Any

from agent.message_content import flatten_message_text
from agent.synthetic_prompt import human_prompt_text

logger = logging.getLogger(__name__)


def note_human_turn_for_skill_review(agent: Any, content: Any) -> None:
    """Remember human-authored input until the next automatic skill review fires.

    Multimodal turns carry their text in content parts, so classify the flattened text; an
    image-only turn has no text and does not count. Classification never fails the turn.
    """
    text = flatten_message_text(content) if isinstance(content, (str, list)) else ""
    try:
        human = human_prompt_text(
            text,
            display_kind=getattr(agent, "_turn_display_kind", None),
            platform=getattr(agent, "platform", None),
        )
    except Exception:
        logger.debug("skill review human-turn classification failed", exc_info=True)
        return
    if human is not None:
        agent._skill_review_human_turn_seen = True


def consume_skill_review_if_due(agent: Any) -> bool:
    """Return whether the skill review is due, consuming its counter and human latch."""
    interval = getattr(agent, "_skill_nudge_interval", 0)
    if not (
        interval > 0
        and getattr(agent, "_iters_since_skill", 0) >= interval
        and getattr(agent, "_skill_review_human_turn_seen", False)
        and "skill_manage" in agent.valid_tool_names
    ):
        return False
    agent._iters_since_skill = 0
    agent._skill_review_human_turn_seen = False
    return True
