from unittest.mock import AsyncMock

import pytest

from agent.topic_icons import choose_topic_icon_deterministic, resolve_override, validate_model_icon
from plugins.platforms.telegram.adapter import TelegramAdapter


def test_semantic_choice_and_recent_rotation():
    allowed = ["🐛", "📊", "💡", "🔒", "📚", "🚀", "✈️", "🛒", "💰"]
    assert choose_topic_icon_deterministic("Fix auth bug", "debug login test", allowed, []) == "🐛"
    assert choose_topic_icon_deterministic("A quiet topic", "", allowed, ["🐛"]) == "📊"


def test_override_and_variation_selector_validation():
    allowed = [{"emoji": "✈️", "custom_emoji_id": "flight"}, {"emoji": "🐛", "custom_emoji_id": "bug"}]
    assert resolve_override("Travel planning", {"travel": "✈️"}, allowed) == "✈️"
    assert validate_model_icon("✈", allowed) == "✈️"
    assert validate_model_icon("🎉", allowed) is None


@pytest.mark.anyio
async def test_adapter_renames_name_and_icon_in_one_bot_call():
    adapter = object.__new__(TelegramAdapter)
    adapter._bot = type("Bot", (), {"edit_forum_topic": AsyncMock()})()
    adapter.name = "test"
    await adapter.rename_dm_topic(42, 7, "Bug triage", icon_custom_emoji_id="bug-id")
    adapter._bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=42, message_thread_id=7, name="Bug triage", icon_custom_emoji_id="bug-id"
    )
