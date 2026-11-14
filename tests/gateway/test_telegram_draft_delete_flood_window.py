"""Draft frames and message deletes stay out of a chat's Telegram flood window.

A 429 ``retry_after`` arms the chat's send flood window; ``send_draft`` and ``delete_message``
must not fire Bot API calls into it (Telegram lengthens a penalty that keeps getting hit), and a
429 on either arms the window for the other paths. SDK-free: the refusal is any exception that
carries ``retry_after``, as PTB's ``RetryAfter`` does.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import flood_state
from plugins.platforms.telegram.adapter import TelegramAdapter


class _Flood(Exception):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"Flood control exceeded. Retry in {seconds} seconds")
        self.retry_after = seconds


def _adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_message_draft = AsyncMock(return_value=True)
    adapter._bot.delete_message = AsyncMock(return_value=True)
    return adapter


@pytest.mark.asyncio
async def test_draft_frames_skip_the_flood_window_a_refused_draft_armed():
    adapter = _adapter()
    adapter._bot.send_message_draft = AsyncMock(side_effect=[_Flood(30), True])

    refused = await adapter.send_draft("4242", 7, "frame 1")
    later = [await adapter.send_draft("4242", 7, f"frame {i}") for i in (2, 3)]

    # Every refusal retains the canonical retry deadline without a second Bot API call.
    assert not refused.success and refused.error.startswith("flood_control:")
    assert refused.retry_after > 0
    assert all(not r.success and r.error.startswith("flood_control:") and r.retry_after > 0 for r in later)
    assert adapter._bot.send_message_draft.await_count == 1
    # The window is the chat's: a send refuses locally too, and another chat still drafts.
    assert (await adapter.send("4242", "hello")).error.startswith("flood_control:")
    assert (await adapter.send_draft("777", 1, "other chat")).raw_response is None
    assert adapter._bot.send_message_draft.await_count == 2


@pytest.mark.asyncio
async def test_delete_refuses_during_the_flood_window_then_deletes(monkeypatch):
    wall = [1000.0]
    monkeypatch.setattr(flood_state, "time", SimpleNamespace(time=lambda: wall[0]))
    adapter = _adapter()
    adapter._record_send_flood_cooldown("4242", 1.0)
    tasks = set(adapter._background_tasks)

    assert await adapter.delete_message("4242", "55") is False
    adapter._bot.delete_message.assert_not_awaited()
    assert set(adapter._background_tasks) == tasks
    wall[0] += 2
    adapter.__dict__["_telegram_send_cooldown_until"].clear()
    adapter.__dict__["_telegram_platform_flood_until"].clear()
    assert await adapter.delete_message("4242", "55") is True
    adapter._bot.delete_message.assert_awaited_once_with(chat_id=4242, message_id=55)


@pytest.mark.asyncio
async def test_long_flood_window_retains_delete_for_caller_retry(monkeypatch):
    wall = [1000.0]
    monkeypatch.setattr(flood_state, "time", SimpleNamespace(time=lambda: wall[0]))
    adapter = _adapter()
    adapter._bot.delete_message = AsyncMock(side_effect=_Flood(60))
    assert await adapter.delete_message("4242", "55") is False
    assert adapter._send_flood_cooldown_remaining("4242") is not None

    adapter._bot.delete_message = AsyncMock(return_value=True)
    tasks = set(adapter._background_tasks)
    assert await adapter.delete_message("4242", "56") is False
    adapter._bot.delete_message.assert_not_awaited()
    assert set(adapter._background_tasks) == tasks
    wall[0] += 61
    adapter.__dict__["_telegram_send_cooldown_until"].clear()
    adapter.__dict__["_telegram_platform_flood_until"].clear()
    assert await adapter.delete_message("4242", "56") is True
    adapter._bot.delete_message.assert_awaited_once_with(chat_id=4242, message_id=56)
