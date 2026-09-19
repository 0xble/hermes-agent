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
    adapter = type("FakeAdapter", (), {})()
    adapter._bot = type("Bot", (), {"edit_forum_topic": AsyncMock()})()
    adapter.name = "test"
    await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage", icon_custom_emoji_id="bug-id")
    adapter._bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=42, message_thread_id=7, name="Bug triage", icon_custom_emoji_id="bug-id"
    )


def test_topic_service_messages_are_routed_to_the_manual_icon_recorder(monkeypatch):
    """forum_topic_created/edited carry no text or media, so they need their own PTB handler
    wired on the StatusUpdate filters. Structural: the telegram package may be mocked by the
    gateway conftest in a mixed run, so PTB classes are stubbed with recorders here."""
    from types import SimpleNamespace
    import plugins.platforms.telegram.adapter as mod
    # Sibling suites force-reimport the adapter module; bind to whatever class is live now.
    TelegramAdapter = mod.TelegramAdapter

    class _Filter:
        def __or__(self, other):
            return _Filter()

    registered: list = []
    fake_filters = SimpleNamespace(
        TEXT=_Filter(), COMMAND=_Filter(), LOCATION=_Filter(), PHOTO=_Filter(), VIDEO=_Filter(), AUDIO=_Filter(),
        VOICE=_Filter(), Document=SimpleNamespace(ALL=_Filter()), Sticker=SimpleNamespace(ALL=_Filter()),
        StatusUpdate=SimpleNamespace(FORUM_TOPIC_CREATED=_Filter(), FORUM_TOPIC_EDITED=_Filter()),
    )
    fake_filters.TEXT.__class__.__invert__ = lambda self: _Filter()
    fake_filters.TEXT.__class__.__and__ = lambda self, other: _Filter()
    monkeypatch.setattr(mod, "filters", fake_filters, raising=False)
    monkeypatch.setattr(mod, "TelegramMessageHandler", lambda flt, cb: registered.append(cb) or object(), raising=False)
    for name in ("CallbackQueryHandler", "InlineQueryHandler", "TypeHandler"):
        monkeypatch.setattr(mod, name, lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(mod, "Update", object, raising=False)

    adapter = object.__new__(TelegramAdapter)
    adapter._manual_topic_icons = {}
    app = SimpleNamespace(add_handler=lambda h, group=0: None)
    TelegramAdapter._register_handlers(adapter, app)
    assert sum(getattr(cb, "__func__", None) is TelegramAdapter._handle_forum_topic_service_message for cb in registered) == 1


@pytest.mark.anyio
async def test_forum_topic_edited_records_manual_icon_for_private_chat():
    from types import SimpleNamespace

    adapter = object.__new__(TelegramAdapter)
    adapter._manual_topic_icons = {}
    message = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"), message_thread_id=7,
        forum_topic_edited=SimpleNamespace(icon_custom_emoji_id="manual-id"), forum_topic_created=None,
    )
    await TelegramAdapter._handle_forum_topic_service_message(adapter, SimpleNamespace(message=message, edited_message=None), None)
    assert TelegramAdapter.get_manual_topic_icon(adapter, "42", "7") == "manual-id"
