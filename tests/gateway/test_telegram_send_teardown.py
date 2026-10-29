"""Text sends must refuse safely if reconnect teardown wins after admission."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import httpx
from telegram.error import BadRequest, NetworkError

from gateway.config import PlatformConfig
from gateway.outbox import _uncertain
from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._rich_send_disabled = True
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    adapter.send_typing = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_send_refuses_teardown_while_waiting_for_chat_lock():
    adapter = _adapter()
    bot = adapter._bot
    lock = adapter._chat_send_lock("123")
    admitted = asyncio.Event()
    original_lock = adapter._chat_send_lock

    def observe_lock(chat_id):
        admitted.set()
        return original_lock(chat_id)

    adapter._chat_send_lock = observe_lock
    async with lock:
        sending = asyncio.create_task(adapter.send("123", "hello"))
        await admitted.wait()
        adapter._send_path_degraded = True
        adapter._bot = None
    result = await sending

    assert not result.success and result.retryable and result.pre_send
    assert result.error == "send_path_degraded"
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["pacing", "network_retry", "ambiguous_retry", "markdown_fallback", "split_tail"])
async def test_send_preserves_certain_tail_when_teardown_wins(monkeypatch, boundary):
    adapter = _adapter()
    bot = adapter._bot
    sent = []

    def teardown():
        adapter._send_path_degraded = True
        adapter._bot = None

    async def send_message(text, **kwargs):
        if boundary == "network_retry":
            raise NetworkError("connect failed") from httpx.ConnectTimeout("connect timed out")
        if boundary == "ambiguous_retry":
            raise NetworkError("connection reset")
        if boundary == "markdown_fallback":
            teardown()
            raise BadRequest("Can't parse entities")
        sent.append(text)
        if boundary == "split_tail":
            teardown()
        return MagicMock(message_id=42)

    async def paced(_seconds):
        teardown()

    bot.send_message = AsyncMock(side_effect=send_message)
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", paced)
    if boundary == "pacing":
        adapter._chat_outbound_slot_remaining = lambda _chat: 1.0
    content = "hello" if boundary != "split_tail" else "hello " * 1500
    result = await adapter.send("123", content, metadata={"notify": True})

    assert not result.success and result.retryable
    if boundary == "ambiguous_retry":
        assert not result.pre_send and _uncertain(result)
        assert result.error == "connection reset"
        assert bot.send_message.await_count == 1
        return
    assert result.error == "send_path_degraded"
    if boundary == "split_tail":
        assert len(sent) == 1
        assert result.raw_response["delivered_message_ids"] == ("42",)
        assert result.raw_response["undelivered_chunks"]
    else:
        assert result.pre_send
        assert bot.send_message.await_count == (0 if boundary == "pacing" else 1)
