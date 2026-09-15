"""Recommendation-only notices stay visible without the generic review heading."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.background_review import (
    _publish_review_summary,
    summarize_background_review_actions,
)


@pytest.mark.parametrize("repeats", [1, 2])
def test_recommendation_notice_is_concise_and_delivered_to_both_surfaces(repeats):
    messages = [
        {
            "role": "tool",
            "content": json.dumps({
                "success": True,
                "observed": True,
                "message": "Skill recommendation recorded; no skill files changed.",
            }),
        }
    ] * repeats
    agent = SimpleNamespace(_safe_print=Mock(), background_review_callback=Mock())

    _publish_review_summary(agent, summarize_background_review_actions(messages, []))

    expected = "Skill recommendation saved for review. No skills changed."
    agent.background_review_callback.assert_called_once_with(expected)
    agent._safe_print.assert_called_once_with(f"  {expected}")


@pytest.mark.parametrize("actions", [
    ["Memory updated"],
    ["Skill 'example' patched"],
    ["Skill recommendation recorded; no skill files changed.", "Skill 'example' patched"],
])
def test_other_review_actions_keep_their_complete_notification(actions):
    agent = SimpleNamespace(_safe_print=Mock(), background_review_callback=Mock())

    _publish_review_summary(agent, actions)

    expected = "💾 Self-improvement review: " + " · ".join(actions)
    agent.background_review_callback.assert_called_once_with(expected)
    agent._safe_print.assert_called_once_with(f"  {expected}")
