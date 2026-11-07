"""Pressure batching through the real progress consumer and Telegram adapter, with no network."""

import asyncio
import logging
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import OUTBOUND_PROGRESS, outbound_class
from gateway.run_turn_runner import TurnRunner
from plugins.platforms.telegram import daily_quota as dq
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

CHAT = "2027045491"


def _runner(tmp_path, used):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="unused"))
    adapter._update_receipt_dir = tmp_path
    adapter._rich_messages_enabled = False
    adapter._rich_send_disabled = True
    adapter._telegram_chat_outbound_slot_secs = 0.0
    adapter._telegram_daily_quota = dq.DailyQuota(call_soft_ceiling=10000)
    for _ in range(used):
        adapter._daily_quota().record(CHAT, "sendChatAction")
    adapter._bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        send_chat_action=AsyncMock(return_value=True),
    )
    runner = TurnRunner.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(
        source=SimpleNamespace(chat_id=CHAT), _progress_reply_to=None, _progress_metadata=None,
        progress_queue=queue.Queue(), last_progress_msg=[None], repeat_count=[0],
        progress_grouping="edit", stream_consumer_holder=[None], _cleanup_progress=False,
        persist_user_display_kind=None,
        _run_still_current=lambda: True, agent_holder=[None], _native_slack_task_cards=False,
    )
    runner._runner = SimpleNamespace(_delivery_adapter_for=lambda source: adapter)
    runner._progress_restore_typing = AsyncMock()
    return runner, adapter


@pytest.mark.parametrize("used, expected", [
    (0, 10), (4999, 10), (5000, 30), (7999, 30), (8000, 60), (9999, 60), (10000, 60), (12000, 60),
])
def test_interval_tracks_the_chats_current_budget(tmp_path, used, expected):
    runner, adapter = _runner(tmp_path, used)
    assert runner._progress_interval_for_chat(adapter) == expected
    # A different chat and a disabled budget retain the normal cadence.
    runner._ctx.source.chat_id = "123"
    assert runner._progress_interval_for_chat(adapter) == 10
    runner._ctx.source.chat_id = CHAT
    adapter._daily_quota().call_soft_ceiling = 0
    assert runner._progress_interval_for_chat(adapter) == 10
    adapter._daily_quota().call_soft_ceiling = 20000
    adapter._daily_quota().note_retry_after(CHAT, 600)
    assert runner._progress_interval_for_chat(adapter) == expected


class ToolDay:
    """Advance virtual time while tool events continue arriving during a batch wait."""

    def __init__(self, runner, duration, events):
        self.runner = runner
        self.t = 100.0
        self.end = self.t + duration
        self.events = [(self.t + at, text) for at, text in events]
        self.waits = []

    async def sleep(self, delay):
        self.waits.append(delay)
        target = min(self.t + delay, self.end)
        while self.events and self.events[0][0] <= target:
            _, text = self.events.pop(0)
            self.runner._progress_emit(text)
        self.t = target
        if self.t >= self.end:
            raise asyncio.CancelledError


@pytest.mark.asyncio
@pytest.mark.parametrize("used, interval", [(0, 10), (5000, 30), (8000, 60)])
async def test_batches_complete_text_and_flushes_at_turn_end(tmp_path, monkeypatch, used, interval):
    import gateway.run_turn_progress as progress

    runner, adapter = _runner(tmp_path, used)
    runner._progress_emit("event0")
    day = ToolDay(runner, 125, [(n, f"event{n}") for n in range(1, 126)])
    monkeypatch.setattr(progress, "time", SimpleNamespace(monotonic=lambda: day.t))
    monkeypatch.setattr(progress, "asyncio", SimpleNamespace(sleep=day.sleep, CancelledError=asyncio.CancelledError))
    token = bind_trigger(SimpleNamespace(text="show tool progress"))
    try:
        await runner.send_progress_messages()
    finally:
        reset_trigger(token)
    edits = [call.kwargs["text"] for call in adapter._bot.edit_message_text.await_args_list]
    # Each update carries the whole accumulated buffer, including events that arrived in the wait.
    assert edits[0].splitlines() == [f"event{n}" for n in range(interval + 1)]
    assert edits[-1].splitlines() == [f"event{n}" for n in range(126)]
    assert day.t == 225  # completion interrupts a 60s batch at 125s without waiting until 180s
    assert max(day.waits) >= interval - 2
    assert len(edits) == 125 // interval + 1
    if used:
        assert len(edits) < 125 // 10  # simulated day: identical tool events, fewer wire edits


@pytest.mark.asyncio
async def test_an_isolated_update_is_sent_when_the_batch_expires(tmp_path, monkeypatch):
    import gateway.run_turn_progress as progress

    runner, adapter = _runner(tmp_path, 0)
    runner._progress_emit("first")
    day = ToolDay(runner, 65, [(1, "second")])
    async def pressure_then_sleep(delay):
        if not day.waits:
            for _ in range(8000):
                adapter._daily_quota().record(CHAT, "sendChatAction")
        await day.sleep(delay)

    monkeypatch.setattr(progress, "time", SimpleNamespace(monotonic=lambda: day.t))
    monkeypatch.setattr(progress, "asyncio", SimpleNamespace(sleep=pressure_then_sleep, CancelledError=asyncio.CancelledError))
    token = bind_trigger(SimpleNamespace(text="show tool progress"))
    try:
        await runner.send_progress_messages()
    finally:
        reset_trigger(token)
    assert adapter._bot.edit_message_text.await_args_list[0].kwargs["text"] == "first\nsecond"
    assert 58 <= max(day.waits) <= 60


def test_hourly_summary_attributes_edits_without_debug_or_counting_them_twice(tmp_path, caplog):
    before_restart = dq.DailyQuota(profile_dir=tmp_path)
    before_restart.record(CHAT, "editMessageText")
    before_restart.flush()
    quota = dq.DailyQuota(profile_dir=tmp_path)
    with outbound_class(OUTBOUND_PROGRESS):
        quota.record(CHAT, "editMessageText")
    quota.record(CHAT, "editMessageText")
    quota.record(CHAT, "sendChatAction")
    with caplog.at_level(logging.INFO, logger=dq.__name__):
        quota.log_summary()
    summary = caplog.records[-1].getMessage()
    assert '"progress":1' in summary
    assert '"unclassified":1' in summary
    assert '"unobserved":1' in summary
    assert '"editMessageText":3' in summary
    assert "4 calls" in summary
