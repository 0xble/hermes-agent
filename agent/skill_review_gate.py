"""Shared eligibility state for automatic background skill reviews."""

from __future__ import annotations

from typing import Any

from agent.synthetic_prompt import human_prompt_text


def note_human_turn_for_skill_review(agent: Any, content: Any) -> None:
    """Remember human-authored input until the next automatic skill review fires."""
    if (
        human_prompt_text(
            content,
            display_kind=getattr(agent, "_turn_display_kind", None),
            platform=getattr(agent, "platform", None),
        )
        is not None
    ):
        agent._skill_review_human_turn_seen = True


def consume_skill_review_if_due(agent: Any) -> bool:
    """Return whether the skill review is due, consuming its counter and human latch."""
    if not (
        agent._skill_nudge_interval > 0
        and agent._iters_since_skill >= agent._skill_nudge_interval
        and getattr(agent, "_skill_review_human_turn_seen", False)
        and "skill_manage" in agent.valid_tool_names
    ):
        return False
    agent._iters_since_skill = 0
    agent._skill_review_human_turn_seen = False
    return True
