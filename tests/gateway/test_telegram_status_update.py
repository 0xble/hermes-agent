"""Tests for TelegramAdapter.send_or_update_status (issue #30045).

The status-update path must:
  1. Send a fresh message on the first call for a (chat_id, status_key) pair.
  2. Edit that same message on subsequent calls with the same key.
  3. Fall back to sending fresh when the cached message edit fails.
  4. Keep distinct keys independent (no cross-talk).
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult


def _install_fake_telegram(monkeypatch):
    """Stub the python-telegram-bot package so TelegramAdapter can be imported."""
    fake_telegram = types.ModuleType("telegram")
    fake_telegram.Update = SimpleNamespace(ALL_TYPES=())
    fake_telegram.Bot = object
    fake_telegram.Message = object
    fake_telegram.InlineKeyboardButton = object
    fake_telegram.InlineKeyboardMarkup = object

    fake_error = types.ModuleType("telegram.error")
    fake_error.NetworkError = type("NetworkError", (Exception,), {})
    fake_error.BadRequest = type("BadRequest", (Exception,), {})
    fake_error.TimedOut = type("TimedOut", (Exception,), {})
    fake_telegram.error = fake_error

    fake_constants = types.ModuleType("telegram.constants")
    fake_constants.ParseMode = SimpleNamespace(MARKDOWN_V2="MarkdownV2")
    fake_constants.ChatType = SimpleNamespace(
        GROUP="group", SUPERGROUP="supergroup",
        CHANNEL="channel", PRIVATE="private",
    )
    fake_telegram.constants = fake_constants

    fake_ext = types.ModuleType("telegram.ext")
    fake_ext.Application = object
    fake_ext.CommandHandler = object
    fake_ext.CallbackQueryHandler = object
    fake_ext.InlineQueryHandler = object
    fake_ext.MessageHandler = object
    fake_ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    fake_ext.filters = object

    fake_request = types.ModuleType("telegram.request")
    fake_request.HTTPXRequest = object

    monkeypatch.setitem(sys.modules, "telegram", fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.error", fake_error)
    monkeypatch.setitem(sys.modules, "telegram.constants", fake_constants)
    monkeypatch.setitem(sys.modules, "telegram.ext", fake_ext)
    monkeypatch.setitem(sys.modules, "telegram.request", fake_request)


@pytest.fixture
def adapter(monkeypatch):
    _install_fake_telegram(monkeypatch)
    from plugins.platforms.telegram.adapter import TelegramAdapter

    a = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    a._bot = MagicMock()
    # Patch send / edit_message so tests can drive them directly.
    a.send = AsyncMock()
    a.edit_message = AsyncMock()
    return a


@pytest.mark.asyncio
async def test_first_call_sends_and_caches_message_id(adapter):
    """First call for a (chat, key) pair must send and remember the id."""
    adapter.send.return_value = SendResult(success=True, message_id="100")

    result = await adapter.send_or_update_status("chat-1", "lifecycle", "starting")

    assert result.success is True
    assert result.message_id == "100"
    adapter.send.assert_awaited_once()
    adapter.edit_message.assert_not_awaited()
    assert adapter._status_message_ids[("chat-1", "", "lifecycle")] == "100"


@pytest.mark.asyncio
async def test_distinct_status_keys_do_not_collide(adapter):
    """A different status_key gets its own message; the original isn't touched."""
    adapter.send.side_effect = [
        SendResult(success=True, message_id="100"),
        SendResult(success=True, message_id="200"),
    ]

    await adapter.send_or_update_status("chat-1", "lifecycle", "ctx pressure")
    await adapter.send_or_update_status("chat-1", "model-switch", "switched to opus")

    assert adapter.send.await_count == 2
    adapter.edit_message.assert_not_awaited()
    assert adapter._status_message_ids[("chat-1", "", "lifecycle")] == "100"
    assert adapter._status_message_ids[("chat-1", "", "model-switch")] == "200"


@pytest.mark.asyncio
async def test_status_after_cleanup_delete_sends_fresh_without_editing(adapter):
    """End-of-turn progress cleanup deletes status bubbles; the next turn's status must not edit the gone id."""
    adapter.send.side_effect = [
        SendResult(success=True, message_id="100"),
        SendResult(success=True, message_id="200"),
    ]
    adapter._bot.delete_message = AsyncMock()

    await adapter.send_or_update_status("chat-1", "lifecycle", "recalled 3 memories")
    assert await adapter.delete_message("chat-1", "100") is True
    result = await adapter.send_or_update_status("chat-1", "lifecycle", "recalled 2 memories")

    adapter.edit_message.assert_not_awaited()
    assert result.message_id == "200"
    assert adapter._status_message_ids[("chat-1", "", "lifecycle")] == "200"


@pytest.mark.asyncio
async def test_same_status_key_in_different_topics_never_edits_another_topic(adapter):
    adapter.send.side_effect = [
        SendResult(success=True, message_id="100"),
        SendResult(success=True, message_id="200"),
    ]
    first = await adapter.send_or_update_status("chat-1", "lifecycle", "topic A", metadata={"thread_id": "101"})
    second = await adapter.send_or_update_status("chat-1", "lifecycle", "topic B", metadata={"thread_id": "202"})

    assert first.message_id != second.message_id
    adapter.edit_message.assert_not_awaited()
    assert adapter._status_message_ids[("chat-1", "101", "lifecycle")] == "100"
    assert adapter._status_message_ids[("chat-1", "202", "lifecycle")] == "200"


@pytest.mark.asyncio
async def test_gateway_turn_status_ownership_survives_topic_and_turn_reuse(adapter):
    """The actual callback and cleanup path must never lend another turn its Telegram bubble."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext

    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda source: adapter
    adapter.send.side_effect = [SendResult(success=True, message_id=str(i)) for i in (100, 200, 300)]
    adapter.edit_message.side_effect = lambda chat, mid, text, **kw: SendResult(success=True, message_id=mid)
    adapter._bot.delete_message = AsyncMock()

    def make_turn(topic):
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", thread_id=topic)
        ctx = TurnContext(source=source, session_key=f"agent:main:telegram:dm:12345:{topic}",
                          _run_still_current=lambda: True, _cleanup_progress=True,
                          _status_adapter=adapter, _status_chat_id="12345",
                          _status_thread_metadata={"thread_id": topic}, user_config={})
        turn = TurnRunner(runner, ctx)
        pending = []
        setattr(turn, "_schedule", lambda coro, log_message, loop=None: pending.append(asyncio.create_task(coro)))
        return ctx, turn, pending

    turns = [make_turn(topic) for topic in ("101", "202", "101")]
    for ctx, turn, pending in turns:
        turn._status_callback_sync("lifecycle", "Working")
        await asyncio.gather(*pending)
    assert len(set(ctx._cleanup_msg_ids[0] for ctx, _, _ in turns)) == len(turns)
    assert adapter.edit_message.await_count == 0

    first_ctx, first_turn, pending = turns[0]
    first_turn._status_callback_sync("lifecycle", "Still working")
    await asyncio.gather(*pending)
    assert adapter.edit_message.await_count == 1

    for ctx, turn, _ in turns:
        runner._run_agent_schedule_bubble_cleanup({"final_response": "done"}, adapter, ctx)
        callback = adapter.pop_post_delivery_callback(ctx.session_key)
        await callback()
    assert len(adapter._bot.delete_message.await_args_list) == len(turns)
    assert not adapter._status_message_ids

    # A send accepted before final delivery can return its receipt after cleanup.
    late_ctx, late_turn, pending = make_turn("303")
    accepted, release = asyncio.Event(), asyncio.Event()

    async def delayed_send(chat, content, metadata=None):
        accepted.set()
        await release.wait()
        return SendResult(success=True, message_id="400")

    adapter.send = AsyncMock(side_effect=delayed_send)
    late_turn._status_callback_sync("lifecycle", "Late status")
    await asyncio.wait_for(accepted.wait(), 5)
    runner._run_agent_schedule_bubble_cleanup({"final_response": "done"}, adapter, late_ctx)
    callback = adapter.pop_post_delivery_callback(late_ctx.session_key)
    await callback()
    release.set()
    await asyncio.gather(*pending)
    await asyncio.gather(*late_turn._status_delivery.tasks)
    assert adapter._bot.delete_message.await_count == len(turns) + 1
    assert "400" not in adapter._status_message_ids.values()
