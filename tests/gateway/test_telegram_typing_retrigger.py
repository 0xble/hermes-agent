"""Telegram post-send typing re-arm is scheduled off the send path and collapsed per chat (#111727).

Awaiting ``sendChatAction`` after every intermediate send ran its TLS round-trip on the same event
loop as the ``getUpdates`` long-polls; under concurrent streaming the polls starved until they
rotted into CLOSE-WAIT while the adapter still reported ``connected``.
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
    # This test models a burst of streamed chunks landing back-to-back; the shared send+edit
    # pacing slot (#116312) would space those sends out to ~1/s, so real time elapses between them
    # and typing re-arms — exactly its collapse cadence. Shut the slot off to keep the burst
    # instantaneous so only the per-chat collapse is measured.
    adapter._telegram_chat_outbound_slot_secs = 0.0
    return adapter


async def _drain(adapter, timeout=3):
    pending = [t for t in adapter._background_tasks if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)


@pytest.mark.asyncio
async def test_send_returns_without_waiting_on_typing():
    """An intermediate send completes while sendChatAction is still stalled."""
    adapter = _make_adapter()
    released = asyncio.Event()

    async def blocking_action(**kwargs):
        await released.wait()

    adapter._bot.send_chat_action = AsyncMock(side_effect=blocking_action)

    result = await asyncio.wait_for(adapter.send("123", "chunk"), timeout=1.0)

    assert result.success is True
    released.set()
    await _drain(adapter)
    assert adapter._bot.send_chat_action.await_count == 1


@pytest.mark.asyncio
async def test_streaming_burst_defers_one_trailing_rearm():
    """A burst gets one immediate and one trailing re-arm per chat."""
    adapter = _make_adapter()

    assert (await adapter.send("123", "chunk 0")).success
    assert (await adapter.send("456", "chunk 0")).success
    await asyncio.sleep(0.5)
    assert (await adapter.send("123", "chunk 1")).success
    assert (await adapter.send("456", "chunk 1")).success
    await asyncio.sleep(0.5)
    assert (await adapter.send("123", "chunk 2")).success
    assert (await adapter.send("456", "chunk 2")).success

    # The trailing re-arm is due two seconds after the immediate one.
    await asyncio.sleep(1.3)
    await _drain(adapter)

    assert adapter._bot.send_chat_action.await_count == 4
    chat_ids = [call.kwargs["chat_id"] for call in adapter._bot.send_chat_action.await_args_list]
    assert [str(chat_id) for chat_id in chat_ids].count("123") == 2
    assert [str(chat_id) for chat_id in chat_ids].count("456") == 2


@pytest.mark.asyncio
async def test_final_reply_cancels_pending_trailing_rearm():
    """The final reply must prevent a deferred intermediate re-arm from firing."""
    adapter = _make_adapter()

    assert (await adapter.send("123", "chunk 0")).success
    await asyncio.sleep(0.5)
    assert (await adapter.send("123", "chunk 1")).success
    await asyncio.sleep(0)
    assert ("123", None) in getattr(adapter, "_telegram_typing_retrigger_trailing_tasks", {})

    assert (await adapter.send("123", "final", metadata={"notify": True})).success
    await asyncio.sleep(2.2)
    await _drain(adapter)

    assert adapter._bot.send_chat_action.await_count == 1


@pytest.mark.asyncio
async def test_retrigger_has_at_most_one_in_flight_per_chat():
    """A trailing re-arm waits for the immediate call instead of overlapping it."""
    adapter = _make_adapter()
    adapter._telegram_typing_retrigger_interval = 0.5
    started = asyncio.Event()
    release = asyncio.Event()
    in_flight = 0
    max_in_flight = 0
    entries = 0

    async def blocking_typing(chat_id, metadata=None):
        nonlocal in_flight, max_in_flight, entries
        entries += 1
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        started.set()
        try:
            await release.wait()
        finally:
            in_flight -= 1

    adapter.send_typing = blocking_typing

    assert (await adapter.send("123", "chunk 0")).success
    await asyncio.wait_for(started.wait(), timeout=1)
    assert (await adapter.send("123", "chunk 1")).success
    # Past the 0.5s window: the trailing re-arm is due but must still be waiting on the first call.
    await asyncio.sleep(0.7)

    assert entries == 1
    assert in_flight == 1

    release.set()
    await _drain(adapter)

    assert entries == 2
    assert max_in_flight == 1
    assert len([t for t in adapter._background_tasks if not t.done()]) == 0


@pytest.mark.asyncio
async def test_zero_interval_still_collapses_a_burst_behind_the_inflight_rearm():
    """0 removes the time window only: a burst during an in-flight re-arm still yields one trailing re-arm."""
    adapter = _make_adapter()
    adapter._telegram_typing_retrigger_interval = 0.0
    started = asyncio.Event()
    release = asyncio.Event()
    entries = 0

    async def blocking_typing(chat_id, metadata=None):
        nonlocal entries
        entries += 1
        started.set()
        await release.wait()

    adapter.send_typing = blocking_typing

    assert (await adapter.send("123", "chunk 0")).success
    await asyncio.wait_for(started.wait(), timeout=1)
    for i in range(1, 5):
        assert (await adapter.send("123", f"chunk {i}")).success
    await asyncio.sleep(0.1)
    assert entries == 1

    release.set()
    await _drain(adapter)

    assert entries == 2
    assert len([t for t in adapter._background_tasks if not t.done()]) == 0


@pytest.mark.asyncio
async def test_zero_interval_rearms_each_spaced_send():
    """With 0 and no re-arm in flight, every intermediate send gets its own immediate re-arm."""
    adapter = _make_adapter()
    adapter._telegram_typing_retrigger_interval = 0.0
    calls = 0

    async def typing(chat_id, metadata=None):
        nonlocal calls
        calls += 1

    adapter.send_typing = typing

    for i in range(3):
        assert (await adapter.send("123", f"chunk {i}")).success
        await _drain(adapter)

    assert calls == 3


@pytest.mark.asyncio
async def test_pause_before_edit_only_finalize_cancels_trailing_rearm():
    """The stream consumer pauses typing, then finalizes by edit (no notify send): no re-arm may follow."""
    adapter = _make_adapter()
    adapter._bot.edit_message_text = AsyncMock(return_value=SimpleNamespace(message_id=1))

    assert (await adapter.send("123", "chunk 0")).success
    await asyncio.sleep(0.5)
    assert (await adapter.send("123", "chunk 1")).success
    await asyncio.sleep(0)
    assert ("123", None) in adapter._telegram_typing_retrigger_trailing_tasks

    adapter.pause_typing_for_chat("123")
    assert not [key for key in adapter._telegram_typing_retrigger_at if key[0] == "123"]
    calls_at_pause = adapter._bot.send_chat_action.await_count
    await adapter.edit_message("123", "1", "final answer", finalize=True)
    await asyncio.sleep(2.2)
    await _drain(adapter)

    assert adapter._bot.send_chat_action.await_count == calls_at_pause


@pytest.mark.asyncio
async def test_request_during_inflight_trailing_send_schedules_another_rearm():
    """A message landing while the trailing re-arm's sendChatAction is in flight gets its own re-arm."""
    adapter = _make_adapter()
    adapter._telegram_typing_retrigger_interval = 0.2
    calls = 0
    trailing_started = asyncio.Event()
    release = asyncio.Event()

    async def typing(chat_id, metadata=None):
        nonlocal calls
        calls += 1
        if calls == 2:  # the trailing re-arm's send
            trailing_started.set()
            await release.wait()

    adapter.send_typing = typing

    assert (await adapter.send("123", "chunk 0")).success
    assert (await adapter.send("123", "chunk 1")).success
    await asyncio.wait_for(trailing_started.wait(), timeout=1)
    # Lands while the trailing send is in flight: must not silently join it.
    assert (await adapter.send("123", "chunk 2")).success
    release.set()
    await asyncio.sleep(0.5)
    await _drain(adapter)

    assert calls == 3


@pytest.mark.asyncio
async def test_final_reply_in_one_topic_keeps_other_topic_rearm():
    """Topic A's final reply must not cancel topic B's pending trailing re-arm in the same chat."""
    adapter = _make_adapter()
    adapter._telegram_typing_retrigger_interval = 0.3
    topics = []

    async def typing(chat_id, metadata=None):
        topics.append((metadata or {}).get("thread_id"))

    adapter.send_typing = typing
    topic_a, topic_b = {"thread_id": "11"}, {"thread_id": "22"}

    assert (await adapter.send("123", "b0", metadata=topic_b)).success
    assert (await adapter.send("123", "b1", metadata=topic_b)).success
    await asyncio.sleep(0)
    assert ("123", "22") in adapter._telegram_typing_retrigger_trailing_tasks

    assert (await adapter.send("123", "a final", metadata={**topic_a, "notify": True})).success
    adapter._cancel_typing_retrigger("123", topic_a)  # _keep_typing's finally for topic A
    await asyncio.sleep(0.5)
    await _drain(adapter)

    assert topics == ["22", "22"]


class _InsertingKey(tuple):
    """A topic key whose comparison inserts into the re-arm maps, like the event loop racing the agent
    thread's pause. Iterating a live map while it grows raises "dictionary changed size"."""

    def __new__(cls, key, maps):
        self = super().__new__(cls, key)
        self.maps = maps
        return self

    def __eq__(self, other):
        for task_map in self.maps:
            task_map[("other", str(len(task_map)))] = None
        return tuple(self) == tuple(other)

    __hash__ = tuple.__hash__


@pytest.mark.asyncio
async def test_cancel_retrigger_survives_concurrent_inserts():
    """pause_typing_for_chat runs on the agent thread; a loop-side insert mid-cancel must not skip it."""
    adapter = _make_adapter()
    tasks, trailing = adapter._typing_retrigger_task_maps()
    key = ("123", "11")
    tasks[key] = asyncio.ensure_future(asyncio.sleep(10))
    trailing[key] = asyncio.ensure_future(asyncio.sleep(10))
    adapter._telegram_typing_retrigger_at[key] = 1.0
    maps = (tasks, trailing, adapter._telegram_typing_retrigger_at)
    adapter._typing_retrigger_key = lambda chat_id, metadata: _InsertingKey(key, maps)
    pending = [tasks[key], trailing[key]]

    adapter._cancel_typing_retrigger("123", {"thread_id": "11"})
    await asyncio.sleep(0)

    assert all(task.cancelled() for task in pending)
    assert key not in tasks and key not in trailing and key not in adapter._telegram_typing_retrigger_at
