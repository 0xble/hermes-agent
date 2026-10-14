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
    """A burst spaced 0.5s apart gets one immediate and one trailing re-arm."""
    adapter = _make_adapter()

    assert (await adapter.send("123", "chunk 0")).success
    await asyncio.sleep(0.5)
    assert (await adapter.send("123", "chunk 1")).success
    await asyncio.sleep(0.5)
    assert (await adapter.send("123", "chunk 2")).success

    # The trailing re-arm is due two seconds after the immediate one.
    await asyncio.sleep(1.3)
    await _drain(adapter)

    assert adapter._bot.send_chat_action.await_count == 2


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
