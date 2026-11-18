"""Topic creation refusals stay actionable and do not spend more Telegram calls.

Regression for https://github.com/tdlib/telegram-bot-api/issues/944.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from agent.incognito import incognito_scope
from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from gateway.telegram_topic_sessions import TopicRequestError, TopicSpec, create_topic_session
from plugins.platforms.telegram.adapter import TelegramAdapter


OUTAGE = (
    "Telegram is refusing new topics right now (BOT_TOPIC_WRONG_HOME, a Telegram server-side outage). "
    "Existing topics still work. Continue in the current topic."
)


@pytest.fixture
def topic_request(monkeypatch):
    # Replace only the icon catalog and transport. The session-create failure path is real.
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    adapter._bot = SimpleNamespace(create_forum_topic=AsyncMock(side_effect=BadRequest("BOT_TOPIC_WRONG_HOME")))
    monkeypatch.setattr(adapter, "get_forum_topic_icon_options", AsyncMock(return_value=[]))
    runner = SimpleNamespace(
        _session_db=None,
        _delivery_adapter_for=lambda _: adapter,
        _sanitize_telegram_topic_title=lambda name: name,
    )
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", thread_id="10")

    async def request():
        with pytest.raises(TopicRequestError) as error:
            await create_topic_session(runner, source, TopicSpec(name="Private project"))
        return str(error.value)

    return adapter, request


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("failure,kind,expected", [
    (None, "bot_unavailable", "Telegram's bot is unavailable right now. Continue in the current topic."),
    ("BOT_TOPIC_WRONG_HOME", "server_refused", OUTAGE),
    ("forums_disabled", "threaded_mode_disabled", "Telegram Threaded Mode is disabled. Enable Threaded Mode in BotFather before creating topics. Continue in the current topic."),
    ("topic_name_duplicate", "duplicate", "Telegram reports that this topic already exists. Use the existing topic instead of retrying creation."),
    ("Private project test-token unexpected failure", "other", "Telegram could not create the topic. Continue in the current topic instead of retrying creation."),
])
async def test_creation_failure_is_specific_and_private_safe(topic_request, private, failure, kind, expected):
    adapter, request = topic_request
    if failure is None:
        adapter._bot = None
    else:
        adapter._bot.create_forum_topic.side_effect = BadRequest(failure)
    with incognito_scope(private):
        message = await request()
    assert message.startswith(expected)
    assert adapter._last_topic_creation_failure.kind == kind
    if kind == "server_refused":
        assert adapter._last_topic_creation_failure.code == "BOT_TOPIC_WRONG_HOME"
    assert "Private project" not in message
    assert "test-token" not in message


@pytest.mark.asyncio
async def test_outage_cooldown_skips_calls_until_expiry(topic_request, monkeypatch):
    adapter, request = topic_request
    now = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    await request()
    bot = adapter._bot
    assert "15 minutes" in await request()
    now[0] += 899
    # A different chat and caller share the adapter's outage deadline too.
    assert await adapter.create_handoff_thread("456", "Another project") is None
    message = await request()
    assert message.startswith(OUTAGE)
    assert "1 second" in message
    assert bot.create_forum_topic.await_count == 1
    bot.create_forum_topic.side_effect = None
    bot.create_forum_topic.return_value = SimpleNamespace(message_thread_id=42)
    now[0] += 1
    assert await adapter._create_dm_topic(456, "Recovered") == 42
    assert bot.create_forum_topic.await_count == 2
    assert adapter._last_topic_creation_failure is None
    # Successful creation leaves the regular path available.
    assert await adapter._create_dm_topic(456, "Next topic") == 42
    assert bot.create_forum_topic.await_count == 3
