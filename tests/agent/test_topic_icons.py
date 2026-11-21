from unittest.mock import AsyncMock

import pytest

from agent.topic_icons import (
    choose_topic_icon_deterministic,
    pick_ranked_icon,
    resolve_override,
    validate_model_icon,
    validate_ranked_icons,
)
from gateway.config import Platform, PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def test_keyword_fallback_prefers_relevance_and_uses_recency_only_to_break_ties():
    allowed = ["💻", "🔥", "📈", "💡", "🪪", "📚", "✈️", "🛒", "💰"]
    assert choose_topic_icon_deterministic("Fix auth bug", "debug the crash", allowed, []) == "🔥"
    # A recently used icon still wins when it is the clearly better match.
    assert choose_topic_icon_deterministic("Fix auth bug", "debug the crash", allowed, ["🔥"]) == "🔥"
    # Equal matches: the one not used recently wins.
    tie = ["📈", "💰"]
    assert choose_topic_icon_deterministic("finance data", "", tie, ["📈"]) == "💰"
    assert choose_topic_icon_deterministic("finance data", "", tie, ["💰"]) == "📈"


def test_keyword_fallback_returns_none_instead_of_an_arbitrary_icon():
    allowed = ["💻", "🔥", "📈", "💡"]
    assert choose_topic_icon_deterministic("A quiet topic", "", allowed, []) is None
    assert choose_topic_icon_deterministic("A quiet topic", "", allowed, ["💻", "🔥"]) is None


def test_override_and_variation_selector_validation():
    allowed = [{"emoji": "✈️", "custom_emoji_id": "flight"}, {"emoji": "🐛", "custom_emoji_id": "bug"}]
    assert resolve_override("Travel planning", {"travel": "✈️"}, allowed) == "✈️"
    assert validate_model_icon("✈", allowed) == "✈️"
    assert validate_model_icon("🎉", allowed) is None


def test_ranked_proposals_keep_only_catalog_icons_in_order():
    allowed = [{"emoji": "✈️", "custom_emoji_id": "flight"}, "🤖", "🧠", "📚", "💻"]
    assert validate_ranked_icons(["🎉", "✈", "🤖", "✈️", "🧠", "📚"], allowed) == ["✈️", "🤖", "🧠"]
    assert validate_ranked_icons("🤖", allowed) == ["🤖"]
    assert validate_ranked_icons(None, allowed) == []
    assert pick_ranked_icon([], ["🤖"]) is None


@pytest.mark.anyio
async def test_adapter_renames_name_and_icon_in_one_bot_call():
    bot = type("Bot", (), {"edit_forum_topic": AsyncMock()})()
    adapter = _retrying_topic_adapter(bot)
    await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage", icon_custom_emoji_id="bug-id")
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=42, message_thread_id=7, name="Bug triage", icon_custom_emoji_id="bug-id"
    )


class _TopicNetworkError(Exception):
    pass


class _TopicTimedOut(_TopicNetworkError):
    pass


class _TopicBadRequest(_TopicNetworkError):
    pass


class _TopicForbidden(Exception):
    pass


class _TopicConnectTimeout(Exception):
    pass


class _TopicRetryAfter(Exception):
    def __init__(self, seconds):
        super().__init__(f"retry after {seconds}")
        self.retry_after = seconds


def _retrying_topic_adapter(bot):
    adapter = object.__new__(TelegramAdapter)
    adapter._bot = bot
    adapter.platform = Platform.TELEGRAM
    adapter._telegram_error_types = lambda: (_TopicNetworkError, _TopicBadRequest, _TopicTimedOut)
    return adapter


@pytest.mark.anyio
async def test_topic_rename_retries_connect_timeout_then_succeeds(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    error = _TopicNetworkError()
    error.__cause__ = _TopicConnectTimeout()
    bot = type("Bot", (), {"edit_forum_topic": AsyncMock(side_effect=[error, None])})()
    adapter = _retrying_topic_adapter(bot)
    sleep = AsyncMock()
    monkeypatch.setattr(adapter_module.asyncio, "sleep", sleep)

    assert await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage") is True
    assert bot.edit_forum_topic.await_count == 2
    sleep.assert_awaited_once_with(1)


@pytest.mark.anyio
async def test_topic_rename_retries_plain_timed_out_then_succeeds(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    bot = type("Bot", (), {"edit_forum_topic": AsyncMock(side_effect=[_TopicTimedOut(), None])})()
    adapter = _retrying_topic_adapter(bot)
    sleep = AsyncMock()
    monkeypatch.setattr(adapter_module.asyncio, "sleep", sleep)

    assert await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage") is True
    assert bot.edit_forum_topic.await_count == 2
    sleep.assert_awaited_once_with(1)


@pytest.mark.anyio
async def test_topic_rename_retries_short_retry_after(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    bot = type("Bot", (), {"edit_forum_topic": AsyncMock(side_effect=[_TopicRetryAfter(0.25), None])})()
    adapter = _retrying_topic_adapter(bot)
    sleep = AsyncMock()
    monkeypatch.setattr(adapter_module.asyncio, "sleep", sleep)

    assert await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage") is True
    assert bot.edit_forum_topic.await_count == 2
    sleep.assert_awaited_once_with(0.25)


@pytest.mark.anyio
async def test_topic_rename_does_not_sleep_for_over_cap_retry_after(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    error = _TopicRetryAfter(6)
    bot = type("Bot", (), {"edit_forum_topic": AsyncMock(side_effect=error)})()
    adapter = _retrying_topic_adapter(bot)
    sleep = AsyncMock()
    monkeypatch.setattr(adapter_module.asyncio, "sleep", sleep)

    with pytest.raises(_TopicRetryAfter) as caught:
        await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage")
    assert caught.value is error
    bot.edit_forum_topic.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.anyio
async def test_topic_rename_does_not_retry_bad_request(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    error = _TopicBadRequest("TOPIC_NOT_FOUND")
    bot = type("Bot", (), {"edit_forum_topic": AsyncMock(side_effect=error)})()
    adapter = _retrying_topic_adapter(bot)
    sleep = AsyncMock()
    monkeypatch.setattr(adapter_module.asyncio, "sleep", sleep)

    with pytest.raises(_TopicBadRequest) as caught:
        await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage")
    assert caught.value is error
    bot.edit_forum_topic.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.anyio
async def test_topic_not_modified_is_an_idempotent_success():
    error = _TopicBadRequest("TOPIC_NOT_MODIFIED")
    bot = type("Bot", (), {"edit_forum_topic": AsyncMock(side_effect=error)})()
    adapter = _retrying_topic_adapter(bot)

    assert await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage") is True
    bot.edit_forum_topic.assert_awaited_once()


@pytest.mark.anyio
async def test_topic_rename_does_not_retry_forbidden(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    error = _TopicForbidden("forbidden")
    bot = type("Bot", (), {"edit_forum_topic": AsyncMock(side_effect=error)})()
    adapter = _retrying_topic_adapter(bot)
    sleep = AsyncMock()
    monkeypatch.setattr(adapter_module.asyncio, "sleep", sleep)

    with pytest.raises(_TopicForbidden) as caught:
        await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage")
    assert caught.value is error
    bot.edit_forum_topic.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.anyio
async def test_topic_rename_raises_the_last_transient_error(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    errors = [_TopicNetworkError("first"), _TopicNetworkError("second"), _TopicNetworkError("last")]
    bot = type("Bot", (), {"edit_forum_topic": AsyncMock(side_effect=errors)})()
    adapter = _retrying_topic_adapter(bot)
    sleep = AsyncMock()
    monkeypatch.setattr(adapter_module.asyncio, "sleep", sleep)

    with pytest.raises(_TopicNetworkError) as caught:
        await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage")
    assert caught.value is errors[-1]
    assert bot.edit_forum_topic.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2]


def test_topic_service_messages_are_routed_to_the_manual_icon_recorder(monkeypatch):
    """forum_topic_created/edited carry no text or media, so they need their own PTB handler
    wired on the StatusUpdate filters. Structural: the telegram package may be mocked by the
    gateway conftest in a mixed run, so PTB classes are stubbed with recorders here."""
    from types import SimpleNamespace
    import plugins.platforms.telegram.adapter as mod
    # Sibling suites force-reimport the adapter module; bind to whatever class is live now.
    TelegramAdapter = mod.TelegramAdapter

    class _Filter:
        def __init__(self, *args, **kwargs):
            pass

        def __or__(self, other):
            return _Filter()

    registered: list = []
    fake_filters = SimpleNamespace(
        TEXT=_Filter(), COMMAND=_Filter(), LOCATION=_Filter(), PHOTO=_Filter(), VIDEO=_Filter(), AUDIO=_Filter(),
        VOICE=_Filter(), VIDEO_NOTE=_Filter(), CONTACT=_Filter(), ALL=_Filter(),
        MessageFilter=_Filter, Document=SimpleNamespace(ALL=_Filter()),
        Sticker=SimpleNamespace(ALL=_Filter()),
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

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._manual_topic_icons = {}
    message = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"), message_thread_id=7,
        forum_topic_edited=SimpleNamespace(icon_custom_emoji_id="manual-id"), forum_topic_created=None,
    )
    await TelegramAdapter._handle_forum_topic_service_message(adapter, SimpleNamespace(message=message, edited_message=None), None)
    assert TelegramAdapter.get_manual_topic_icon(adapter, "42", "7") == "manual-id"


@pytest.mark.anyio
async def test_bots_own_topic_edit_echo_is_not_recorded_as_manual():
    from types import SimpleNamespace

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._manual_topic_icons = {}
    adapter._auto_topic_icons_written = {}
    adapter._bot = type("Bot", (), {"edit_forum_topic": AsyncMock()})()
    adapter.platform = type("P", (), {"value": "telegram"})()
    await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage", icon_custom_emoji_id="auto-id")
    echo = SimpleNamespace(chat=SimpleNamespace(id=42, type="private"), message_thread_id=7,
                           forum_topic_edited=SimpleNamespace(icon_custom_emoji_id="auto-id"), forum_topic_created=None)
    await TelegramAdapter._handle_forum_topic_service_message(adapter, SimpleNamespace(message=echo, edited_message=None), None)
    assert TelegramAdapter.get_manual_topic_icon(adapter, "42", "7") is None
    user_pick = SimpleNamespace(chat=SimpleNamespace(id=42, type="private"), message_thread_id=7,
                                forum_topic_edited=SimpleNamespace(icon_custom_emoji_id="user-id"), forum_topic_created=None)
    await TelegramAdapter._handle_forum_topic_service_message(adapter, SimpleNamespace(message=user_pick, edited_message=None), None)
    assert TelegramAdapter.get_manual_topic_icon(adapter, "42", "7") == "user-id"


@pytest.mark.anyio
async def test_post_connect_housekeeping_warms_the_icon_catalog_only_when_auto_icons_are_on():
    from types import SimpleNamespace

    for enabled in (True, False):
        adapter = object.__new__(TelegramAdapter)
        adapter.config = SimpleNamespace(extra={"auto_topic_icons": enabled})
        adapter._post_connect_task = None
        adapter._register_command_menu = AsyncMock()
        adapter._set_status_indicator = AsyncMock()
        adapter._setup_dm_topics = AsyncMock()
        adapter.get_forum_topic_icon_options = AsyncMock(return_value=[])
        await TelegramAdapter._run_post_connect_housekeeping(adapter)
        assert adapter.get_forum_topic_icon_options.await_count == int(enabled)


@pytest.mark.anyio
async def test_icon_write_clears_the_remembered_manual_icon():
    adapter = object.__new__(TelegramAdapter)
    adapter._manual_topic_icons = {"42:7": "user-id"}
    adapter._auto_topic_icons_written = {}
    adapter._bot = type("Bot", (), {"edit_forum_topic": AsyncMock()})()
    adapter.platform = type("P", (), {"value": "telegram"})()
    await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage")
    assert TelegramAdapter.get_manual_topic_icon(adapter, "42", "7") == "user-id"  # name-only edit keeps it
    await TelegramAdapter.rename_dm_topic(adapter, 42, 7, "Bug triage", icon_custom_emoji_id="auto-id")
    assert TelegramAdapter.get_manual_topic_icon(adapter, "42", "7") is None
