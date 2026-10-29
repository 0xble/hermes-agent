"""A deleted private topic must not strand a completed Telegram reply."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
async def test_deleted_private_topic_delivers_final_at_chat_root():
    from telegram.error import BadRequest

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    send_message = AsyncMock(side_effect=[
        BadRequest("Message thread not found"),
        SimpleNamespace(message_id=120),
    ])
    adapter._bot = SimpleNamespace(send_message=send_message)
    result = await adapter._send_with_retry(
        chat_id="2027045491", content="Completed response", reply_to="150565",
        metadata={
            "notify": True, "thread_id": "150565",
            "telegram_reply_to_message_id": "150565",
            "telegram_dm_topic_reply_fallback": True,
        },
    )
    assert result.success is True
    assert result.message_id == "120"
    assert send_message.await_count == 2
    root = send_message.await_args_list[1].kwargs
    assert root["reply_to_message_id"] is None
    assert root["message_thread_id"] is None
    assert root["text"].startswith("Recovered response from a deleted Telegram topic:")
    assert adapter.is_dm_topic_stale("2027045491", "150565") is True


@pytest.mark.asyncio
async def test_partial_final_recovers_without_repeating_text_and_interim_stays_in_topic():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter.send = AsyncMock(side_effect=[
        SendResult(success=False, error="Bad Request: message thread not found",
                   raw_response={"partial_overflow": True, "delivered_chunks": 1,
                                 "total_chunks": 2, "undelivered_chunks": ("SECOND",)}),
        SendResult(success=True, message_id="root-notice"),
        SendResult(success=False, error="Bad Request: message thread not found"),
        SendResult(success=False, error="Bad Request: message thread not found"),
    ])
    metadata = {
        "notify": True, "thread_id": "150565", "telegram_reply_to_message_id": "150565",
        "telegram_dm_topic_reply_fallback": True,
    }
    result = await adapter._send_with_retry(
        "2027045491", "FIRST SECOND", reply_to="150565", metadata=metadata)
    assert result.success is True
    root = adapter.send.await_args_list[1].kwargs
    assert root["content"] == (
        "A response was partially delivered before its Telegram topic was deleted. "
        "The complete response remains in Hermes session history."
    )
    assert root["reply_to"] is None
    assert "thread_id" not in root["metadata"]

    interim = await adapter._send_with_retry(
        "2027045491", "Interim commentary", metadata={**metadata, "_interim_send": True})
    assert interim.success is False
    assert all(call.kwargs.get("metadata", {}).get("thread_id") == "150565"
               for call in adapter.send.await_args_list[2:])
