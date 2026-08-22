from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
async def test_transient_private_topic_error_retries_before_marking_stale():
    from telegram.error import BadRequest

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    send_message = AsyncMock(
        side_effect=[
            BadRequest("Message thread not found"),
            SimpleNamespace(message_id=42),
        ]
    )
    adapter._bot = SimpleNamespace(send_message=send_message)
    prune = MagicMock()
    adapter._prune_stale_dm_topic_binding = prune

    result = await adapter.send(
        chat_id="2027045491",
        content="Completed response",
        reply_to="150565",
        metadata={
            "notify": True,
            "thread_id": "150565",
            "telegram_reply_to_message_id": "150565",
            "telegram_dm_topic_reply_fallback": True,
        },
    )

    assert result.success is True
    assert send_message.await_count == 2
    assert adapter.is_dm_topic_stale("2027045491", "150565") is False
    prune.assert_not_called()


@pytest.mark.asyncio
async def test_deleted_private_topic_recovers_final_response_at_chat_root(monkeypatch):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    calls = []

    async def fake_send(chat_id, content, reply_to=None, metadata=None):
        calls.append({
            "chat_id": chat_id,
            "content": content,
            "reply_to": reply_to,
            "metadata": dict(metadata or {}),
        })
        if len(calls) == 1:
            return SendResult(
                success=False,
                error="Bad Request: message thread not found",
                retryable=False,
            )
        return SendResult(success=True, message_id="root-message")

    monkeypatch.setattr(adapter, "send", fake_send)
    prune = MagicMock()
    monkeypatch.setattr(adapter, "_prune_stale_dm_topic_binding", prune)

    result = await adapter._send_with_retry(
        chat_id="2027045491",
        content="Completed response",
        reply_to="150565",
        metadata={
            "notify": True,
            "thread_id": "150565",
            "message_thread_id": "150565",
            "telegram_reply_to_message_id": "150565",
            "telegram_dm_topic_reply_fallback": True,
        },
    )

    assert result.success is True
    assert result.message_id == "root-message"
    assert calls[1] == {
        "chat_id": "2027045491",
        "content": "Recovered response from a deleted Telegram topic:\n\nCompleted response",
        "reply_to": None,
        "metadata": {
            "notify": True,
            "telegram_stale_topic_recovery": True,
        },
    }
    prune.assert_called_once_with("2027045491", "150565")
    assert adapter.is_dm_topic_stale("2027045491", "150565") is True


@pytest.mark.asyncio
async def test_deleted_private_topic_does_not_recover_interim_message(monkeypatch):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    calls = []

    async def fake_send(chat_id, content, reply_to=None, metadata=None):
        calls.append(dict(metadata or {}))
        return SendResult(
            success=False,
            error="Bad Request: message thread not found",
            retryable=False,
        )

    monkeypatch.setattr(adapter, "send", fake_send)

    result = await adapter._send_with_retry(
        chat_id="2027045491",
        content="Interim commentary",
        metadata={"thread_id": "150565", "_interim_send": True},
    )

    assert result.success is False
    assert len(calls) == 2
    assert calls[1]["thread_id"] == "150565"


@pytest.mark.asyncio
async def test_partial_topic_delivery_recovers_with_notice_not_duplicate_content(monkeypatch):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    calls = []

    async def fake_send(chat_id, content, reply_to=None, metadata=None):
        calls.append({"content": content, "metadata": dict(metadata or {})})
        if len(calls) == 1:
            return SendResult(
                success=False,
                error="Bad Request: message thread not found",
                raw_response={
                    "telegram_stale_topic_partial_delivery": True,
                    "delivered_chunks": 1,
                    "total_chunks": 2,
                },
            )
        return SendResult(success=True, message_id="root-notice")

    monkeypatch.setattr(adapter, "send", fake_send)
    monkeypatch.setattr(adapter, "_prune_stale_dm_topic_binding", MagicMock())

    result = await adapter._send_with_retry(
        chat_id="2027045491",
        content="FIRST CHUNK SECOND CHUNK",
        reply_to="150565",
        metadata={
            "notify": True,
            "thread_id": "150565",
            "telegram_reply_to_message_id": "150565",
            "telegram_dm_topic_reply_fallback": True,
        },
    )

    assert result.success is True
    assert calls[1]["content"] == (
        "A response was partially delivered before its Telegram topic was deleted. "
        "The complete response remains in Hermes session history."
    )
    assert "FIRST CHUNK" not in calls[1]["content"]


@pytest.mark.asyncio
async def test_root_recovery_failure_does_not_reenter_stale_topic_recovery(monkeypatch):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    calls = []

    async def fake_send(chat_id, content, reply_to=None, metadata=None):
        calls.append(dict(metadata or {}))
        if len(calls) == 1:
            return SendResult(
                success=False,
                error="Bad Request: message thread not found",
            )
        return SendResult(
            success=False,
            error="Request timed out with unknown delivery state",
        )

    monkeypatch.setattr(adapter, "send", fake_send)
    monkeypatch.setattr(adapter, "_prune_stale_dm_topic_binding", MagicMock())

    result = await adapter._send_with_retry(
        chat_id="2027045491",
        content="Completed response",
        reply_to="150565",
        metadata={
            "notify": True,
            "thread_id": "150565",
            "telegram_reply_to_message_id": "150565",
            "telegram_dm_topic_reply_fallback": True,
        },
    )

    assert result.success is False
    assert len(calls) == 2
    assert "thread_id" not in calls[1]
    assert calls[1]["telegram_stale_topic_recovery"] is True
