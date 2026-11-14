"""Pressure batching through the real progress consumer and Telegram adapter, with no network."""

import asyncio
import logging
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import OUTBOUND_PROGRESS, SendResult, outbound_class
from gateway.run_turn_runner import TurnRunner
from plugins.platforms.telegram import daily_quota as dq
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger
import itertools

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
            if isinstance(text, tuple):
                self.runner._ctx.progress_queue.put(text)
            else:
                self.runner._progress_emit(text)
        self.t = target
        if self.t >= self.end:
            raise asyncio.CancelledError


def _patch_day(monkeypatch, day, sleep=None):
    import gateway.run_turn_progress as progress
    import gateway.run_turn_runner as runner

    clock = SimpleNamespace(monotonic=lambda: day.t)
    monkeypatch.setattr(progress, "time", clock)
    monkeypatch.setattr(runner, "time", clock)
    monkeypatch.setattr(progress, "asyncio", SimpleNamespace(
        sleep=sleep or day.sleep, CancelledError=asyncio.CancelledError))


@pytest.mark.asyncio
@pytest.mark.parametrize("used, interval", [(0, 10), (5000, 30), (8000, 60)])
async def test_batches_complete_text_and_flushes_at_turn_end(tmp_path, monkeypatch, used, interval):
    runner, adapter = _runner(tmp_path, used)
    runner._progress_emit("event0")
    day = ToolDay(runner, 125, [(n, f"event{n}") for n in range(1, 126)])
    _patch_day(monkeypatch, day)
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
    runner, adapter = _runner(tmp_path, 0)
    runner._progress_emit("first")
    day = ToolDay(runner, 65, [(1, "second")])
    async def pressure_then_sleep(delay):
        if not day.waits:
            for _ in range(8000):
                adapter._daily_quota().record(CHAT, "sendChatAction")
        await day.sleep(delay)

    _patch_day(monkeypatch, day, pressure_then_sleep)
    token = bind_trigger(SimpleNamespace(text="show tool progress"))
    try:
        await runner.send_progress_messages()
    finally:
        reset_trigger(token)
    assert adapter._bot.edit_message_text.await_args_list[0].kwargs["text"] == "first\nsecond"
    assert 58 <= max(day.waits) <= 60


@pytest.mark.asyncio
async def test_flood_batch_retries_transient_failures_with_backoff(tmp_path, monkeypatch):
    runner, adapter = _runner(tmp_path, 0)
    runner._progress_emit("first")
    day = ToolDay(runner, 105, [(1, "second"), (15, "third")])
    _patch_day(monkeypatch, day)
    attempts = []

    async def edit(**kwargs):
        attempts.append((day.t, kwargs["content"]))
        if len(attempts) == 1:
            return SendResult(success=False, retryable=True, retry_after=12, error="flood")
        if len(attempts) <= 4:
            return SendResult(success=False, retryable=True, error="Not connected")
        # Bound an unfixed busy loop without relying on a wall-clock timeout.
        runner._ctx._run_still_current = lambda: False
        return SendResult(success=True, message_id="1")

    adapter.edit_message = AsyncMock(side_effect=edit)
    await runner.send_progress_messages()
    times = [at for at, _ in attempts]
    assert len(attempts) == 5  # The isolated batch retries through recovery, without new events.
    assert times[1] >= times[0] + 12
    assert [b - a for a, b in itertools.pairwise(times)] == [12, 10, 20, 40]
    assert all(text == "first\nsecond\nthird" for _, text in attempts[1:])
    assert adapter._bot.send_message.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    SendResult(success=False, retryable=True, error="Not connected"),
    SendResult(success=True, deferred=True),
])
async def test_initial_sends_without_an_anchor_keep_the_normal_floor(tmp_path, monkeypatch, result):
    runner, adapter = _runner(tmp_path, 8000)
    runner._progress_emit("event0")
    day = ToolDay(runner, 35, [(n, f"event{n}") for n in range(1, 36)])
    _patch_day(monkeypatch, day)
    attempts = []

    async def send(**kwargs):
        attempts.append((day.t, kwargs["content"]))
        return result

    adapter.send = AsyncMock(side_effect=send)
    await runner.send_progress_messages()
    assert [at for at, _ in attempts] == [100, 110, 120, 130]
    assert attempts[1][1].splitlines() == [f"event{n}" for n in range(11)]
    adapter._bot.edit_message_text.assert_not_awaited()


def test_progress_interval_falls_back_when_quota_read_fails(tmp_path, monkeypatch):
    runner, adapter = _runner(tmp_path, 8000)

    def unavailable():
        raise OSError("quota unavailable")

    monkeypatch.setattr(adapter, "_daily_quota", unavailable)
    assert runner._progress_interval_for_chat(adapter) == adapter.PROGRESS_EDIT_INTERVAL


@pytest.mark.asyncio
async def test_overflow_during_a_batch_keeps_the_newest_bubble_editable(tmp_path, monkeypatch):
    runner, adapter = _runner(tmp_path, 0)
    monkeypatch.setattr(adapter, "MAX_MESSAGE_LENGTH", 120)
    adapter._bot.send_message.side_effect = [SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]
    lines = [letter * 45 for letter in "abcd"]
    runner._progress_emit(lines[0])
    day = ToolDay(runner, 25, [(1, lines[1]), (2, lines[2]), (12, lines[3])])
    _patch_day(monkeypatch, day)
    await runner.send_progress_messages()
    sends = [call.kwargs for call in adapter._bot.send_message.await_args_list]
    edits = [call.kwargs for call in adapter._bot.edit_message_text.await_args_list]
    assert [call["text"] for call in sends] == [lines[0], lines[2]]
    assert edits[0]["message_id"] == 1
    assert edits[0]["text"] == "\n".join(lines[:2])
    assert edits[-1]["message_id"] == 2
    assert edits[-1]["text"] == "\n".join(lines[2:])
    assert all(len(call["text"]) <= 120 for call in sends + edits)


@pytest.mark.asyncio
async def test_reset_during_a_batch_preserves_lines_on_each_side(tmp_path, monkeypatch):
    runner, adapter = _runner(tmp_path, 0)
    adapter._bot.send_message.side_effect = [SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]
    runner._progress_emit("before0")
    day = ToolDay(runner, 25, [
        (1, "before1"), (2, ("__reset__",)), (3, "after0"), (12, "after1"),
    ])
    _patch_day(monkeypatch, day)
    await runner.send_progress_messages()
    sends = [call.kwargs["text"] for call in adapter._bot.send_message.await_args_list]
    edits = [call.kwargs for call in adapter._bot.edit_message_text.await_args_list]
    assert sends == ["before0", "after0"]
    assert edits[0]["message_id"] == 1
    assert edits[0]["text"] == "before0\nbefore1"
    assert edits[-1]["message_id"] == 2
    assert edits[-1]["text"] == "after0\nafter1"


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_path", ["consumer", "batch"])
async def test_reset_does_not_edit_an_already_published_buffer(tmp_path, monkeypatch, reset_path):
    runner, adapter = _runner(tmp_path, 0)
    runner._progress_emit("first")
    day = ToolDay(runner, 15, [(1, ("__reset__",))])
    _patch_day(monkeypatch, day)
    if reset_path == "consumer":
        await runner.send_progress_messages()
    else:
        st = runner._progress_edit_state(adapter)
        runner._progress_absorb(st, runner._ctx.progress_queue.get_nowait())
        await runner._progress_send_or_edit(st, "first")
        runner._ctx.progress_queue.put(("__reset__",))
        await runner._collect_progress_batch(st)
    adapter._bot.send_message.assert_awaited_once()
    adapter._bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_flush_is_not_repeated_but_failed_flush_keeps_new_text(tmp_path):
    runner, adapter = _runner(tmp_path, 0)
    st = runner._progress_edit_state(adapter)
    runner._progress_absorb(st, "first")
    await runner._progress_send_or_edit(st, "first")
    runner._progress_absorb(st, "second")
    adapter.edit_message = AsyncMock(side_effect=[
        SendResult(success=False, retryable=True), SendResult(success=True, message_id="1"),
    ])
    await runner._flush_progress_edit(st)
    await runner._flush_progress_edit(st)
    await runner._flush_progress_edit(st)
    assert adapter.edit_message.await_count == 2
    assert all(call.kwargs["content"] == "first\nsecond"
               for call in adapter.edit_message.await_args_list)


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


@pytest.mark.asyncio
async def test_transient_edit_attempts_back_off_to_a_cap_and_reset_on_recovery(tmp_path, monkeypatch):
    runner, adapter = _runner(tmp_path, 0)
    runner._progress_emit("first")
    day = ToolDay(runner, 245, [(1, "second"), (105, "third"), (205, "fourth")])
    _patch_day(monkeypatch, day)
    attempts = []

    async def edit(**kwargs):
        attempts.append((day.t, kwargs["content"]))
        return SendResult(success=len(attempts) == 6, retryable=True, message_id="1")

    adapter.edit_message = AsyncMock(side_effect=edit)
    await runner.send_progress_messages()
    # 10s floor, doubling to a 60s cap. A successful edit resets the next retry to 10s.
    assert [at for at, _ in attempts] == [110, 120, 140, 180, 240, 300, 310, 320, 340]
    assert all(text == "first\nsecond" for _, text in attempts[:4])
    assert all(text == "first\nsecond\nthird" for _, text in attempts[4:6]), attempts
    assert all(text == "first\nsecond\nthird\nfourth" for _, text in attempts[6:])
    adapter._bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [["after stop"], [("__reset__",), "after stop"]])
async def test_interrupted_batch_drops_queued_lines_and_reset_markers(tmp_path, queued):
    runner, adapter = _runner(tmp_path, 0)
    st = runner._progress_edit_state(adapter)
    runner._progress_absorb(st, "first")
    await runner._progress_send_or_edit(st, "first")
    runner._progress_absorb(st, "pending")
    runner._ctx.agent_holder[0] = SimpleNamespace(is_interrupted=True)
    for raw in queued:
        runner._ctx.progress_queue.put(raw)
    await runner._collect_progress_batch(st)
    assert runner._ctx.progress_queue.empty()
    assert st.progress_lines == ["first", "pending"]
    adapter._bot.send_message.assert_awaited_once()
    adapter._bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupt_during_batch_wait_does_not_render_or_flush_on_cancel(tmp_path, monkeypatch):
    runner, adapter = _runner(tmp_path, 0)
    agent = SimpleNamespace(is_interrupted=False)
    runner._ctx.agent_holder[0] = agent
    runner._progress_emit("first")
    day = ToolDay(runner, 15, [(1, "second"), (2, "third")])

    async def stop_during_wait(delay):
        await day.sleep(delay)
        if day.t >= 110:
            agent.is_interrupted = True

    _patch_day(monkeypatch, day, stop_during_wait)
    await runner.send_progress_messages()
    assert runner._ctx.progress_queue.empty()
    adapter._bot.send_message.assert_awaited_once()
    adapter._bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_separate_grouping_keeps_the_normal_floor_under_pressure(tmp_path, monkeypatch):
    runner, adapter = _runner(tmp_path, 8000)
    runner._ctx.progress_grouping = "separate"
    runner._progress_emit("first")
    day = ToolDay(runner, 35, [(1, "second"), (2, "third"), (3, "fourth")])
    _patch_day(monkeypatch, day)
    attempts = []

    async def send(**kwargs):
        attempts.append((day.t, kwargs["content"]))
        return SendResult(success=True, message_id=str(len(attempts)))

    adapter.send = AsyncMock(side_effect=send)
    await runner.send_progress_messages()
    assert attempts == [(100, "first"), (110, "second"), (120, "third"), (130, "fourth")]
    adapter._bot.edit_message_text.assert_not_awaited()
