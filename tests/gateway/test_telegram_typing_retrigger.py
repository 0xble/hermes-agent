"""Telegram post-send typing re-arm is scheduled off the send path and collapsed per chat (#111727).

Awaiting ``sendChatAction`` after every intermediate send ran its TLS round-trip on the same event
loop as the ``getUpdates`` long-polls; under concurrent streaming the polls starved until they
rotted into CLOSE-WAIT while the adapter still reported ``connected``.

Whether a scheduled re-arm reaches Telegram is the chat's send gate's decision, not this path's:
HERMES-084 puts ``sendChatAction`` inside the same per-chat budget as real sends, because an
unbudgeted typing loop was itself a flood source. These tests pin the scheduling contract — off the
send path, at most one in-flight re-arm per chat, at most one per interval.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._rich_messages_enabled = False
    adapter._bot = AsyncMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    adapter._bot.send_chat_action = AsyncMock(return_value=None)
    return adapter


async def _drain(adapter):
    pending = [t for t in adapter._background_tasks if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=2)


@pytest.mark.asyncio
async def test_send_returns_without_waiting_on_typing():
    """An intermediate send completes while the typing re-arm is still stalled."""
    adapter = _make_adapter()
    released = asyncio.Event()

    async def blocking_typing(*_args, **_kwargs):
        await released.wait()

    adapter.send_typing = blocking_typing

    result = await asyncio.wait_for(adapter.send("123", "chunk"), timeout=1.0)

    assert result.success is True
    # The send returned while the re-arm is still in flight: it was scheduled, never awaited.
    in_flight = adapter._telegram_typing_retrigger_tasks.get("123")
    assert in_flight is not None and not in_flight.done()
    released.set()
    await _drain(adapter)


@pytest.mark.asyncio
async def test_streaming_burst_collapses_to_one_chat_action_per_chat():
    """Every streamed chunk re-arms typing; within the interval that is one re-arm per chat."""
    adapter = _make_adapter()
    released = asyncio.Event()
    started: list[str] = []

    async def blocking_typing(chat_id, metadata=None):
        started.append(str(chat_id))
        await released.wait()

    adapter.send_typing = blocking_typing

    for _ in range(20):
        await adapter._retrigger_typing("123", None)
        await adapter._retrigger_typing("456", None)

    await asyncio.sleep(0)  # let the scheduled re-arms start
    # 40 re-arm requests, two chats: one in-flight task each, so one chat action per chat.
    assert sorted(started) == ["123", "456"]
    released.set()
    await _drain(adapter)
