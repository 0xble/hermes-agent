"""Offline mixed Telegram traffic: one shared chat gate, final priority, no flood bypass."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import RetryAfter, TimedOut

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def adapter():
    a = TelegramAdapter(PlatformConfig(enabled=True, token="fake", extra={"rich_messages": False}))
    a._send_cooldown_seconds = 0.02
    a._send_cooldown_max_wait = 0.3
    a._bot = MagicMock()
    a._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    a._bot.edit_message_text = AsyncMock(return_value=SimpleNamespace(message_id=7))
    a._bot.do_api_request = AsyncMock(return_value=True)
    a._bot.send_message_draft = AsyncMock(return_value=True)
    a._bot.delete_message = AsyncMock(return_value=True)
    a._bot.send_chat_action = AsyncMock(return_value=True)
    a._record_rich_sent = MagicMock()
    return a


@pytest.mark.asyncio
async def test_long_rich_flood_stops_legacy_draft_overflow_delete_and_topic_requests():
    a = adapter()
    a._bot.do_api_request.side_effect = RetryAfter(3600)
    result = await a._try_edit_rich("42", "7", "rich")
    assert not result.success and result.retryable and result.retry_after >= 3599
    assert a._send_cooldown_until["42"] - time.monotonic() >= 3599
    assert not await a._try_send_rich_draft("42", 1, "draft", {"thread_id": "8"})
    result = await a.send_draft("42", 1, "legacy draft", {"thread_id": "9"})
    assert not result.success
    assert not (await a.edit_message("42", "7", "legacy edit", finalize=True)).success
    assert not (await a.send("42", "final", metadata={"thread_id": "9"})).success
    assert await a._send_overflow_continuation("42", "continuation", 7, {}, None, {}, True) is None
    assert not await a.delete_message("42", "7")
    assert await a._create_dm_topic(42, "test") is None
    assert a._bot.do_api_request.await_count == 1
    a._bot.send_message.assert_not_awaited()
    a._bot.edit_message_text.assert_not_awaited()
    a._bot.send_message_draft.assert_not_awaited()
    a._bot.delete_message.assert_not_awaited()
    a._bot.create_forum_topic.assert_not_called()
    # Evidence supports a chat-local deadline, not inventing a bot-global ban.
    assert (await a.send("43", "other chat")).success


@pytest.mark.asyncio
async def test_final_reply_overtakes_status_waiting_for_cooldown():
    a = adapter()
    a._send_cooldown_until["42"] = time.monotonic() + 0.08
    status = asyncio.create_task(a.edit_message("42", "7", "expendable", finalize=True,
                                               metadata={"hermes_status": True, "thread_id": "8"}))
    # Let status acquire the chat lock and begin its shared cooldown wait.
    await asyncio.sleep(0.01)
    final = asyncio.create_task(a.send("42", "final answer", metadata={"thread_id": "9"}))
    status_result, final_result = await asyncio.gather(status, final)
    assert not status_result.success and status_result.retryable
    assert final_result.success
    a._bot.edit_message_text.assert_not_awaited()
    assert "final answer" in a._bot.send_message.call_args.kwargs["text"]
    assert not a._send_final_waiters


@pytest.mark.asyncio
async def test_mixed_rich_and_legacy_requests_share_topic_budget():
    a = adapter()
    calls = []
    async def raw(*args, **kwargs):
        calls.append(("rich", time.monotonic()))
        return True
    async def message(**kwargs):
        calls.append(("legacy", time.monotonic()))
        return SimpleNamespace(message_id=7)
    a._bot.do_api_request.side_effect = raw
    a._bot.send_message.side_effect = message
    assert await a._try_send_rich_draft("42", 1, "preview", {"thread_id": "8"})
    assert (await a.send("42", "final", metadata={"thread_id": "9"})).success
    assert (await a._try_edit_rich("42", "7", "edit")).success
    assert [c[0] for c in calls] == ["rich", "legacy", "rich"]
    assert all(b[1] - x[1] >= 0.018 for x, b in zip(calls, calls[1:]))


@pytest.mark.asyncio
async def test_startup_seed_and_cleanup_observe_shared_deadline(monkeypatch):
    a = adapter()
    a._dm_topics_config = [{"chat_id": 42, "topics": [{"name": "test"}]}]
    a._create_dm_topic = AsyncMock(return_value=8)
    a._persist_dm_topic_thread_id = MagicMock()
    a._send_cooldown_until["42"] = time.monotonic() + 3600
    await a._setup_dm_topics()
    a._bot.send_message.assert_not_awaited()
    assert not await a.delete_message("42", "7")
    a._bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_overflow_ambiguous_timeout_never_plain_resends():
    a = adapter()
    a._bot.send_message.side_effect = TimedOut()
    assert await a._send_overflow_continuation("42", "content", 7, {}, None, {}, True) is None
    a._bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_remains_expendable_behind_pending_final():
    a = adapter()
    assert a.deletion_retry_after('42') == 0
    a._send_final_waiters = {'42': 1}
    assert a.deletion_retry_after('42') > 0
    assert not await a.delete_message('42', '7')
    a._bot.delete_message.assert_not_awaited()
    a._send_final_waiters['42'] = 0
    assert await a.delete_message('42', '7')
    a._bot.delete_message.assert_awaited_once()
