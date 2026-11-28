"""``/stop`` and ``/new`` that reach the runner while a turn runs drop the burst the adapter holds.

A turn admitted to the session FIFO behind another one runs outside the adapter's session guard.
The adapter then sees an idle chat, so its own busy discard never runs, and the runner sees a
running turn, so the idle dispatch never runs either. The photo the adapter was still buffering
became a turn after the command (#81370).
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


def _event(text: str, chat_id: str, message_type: MessageType = MessageType.TEXT) -> MessageEvent:
    return MessageEvent(
        text=text, message_id=f"m-{chat_id}", message_type=message_type,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm", user_id=chat_id))


def _buffer_photo(adapter, chat_id: str):
    """Put one photo in the adapter's burst buffer, as an inbound photo does; returns its flush task."""
    event = _event("old photo caption", chat_id, MessageType.PHOTO)
    key = adapter._photo_batch_key(event, SimpleNamespace(media_group_id=None))
    adapter._enqueue_photo_event(key, event)
    return adapter._pending_photo_batch_tasks[key]


@pytest_asyncio.fixture
async def rig():
    """A real Telegram adapter holding one photo for chat 42 and one for chat 7, and the runner's
    busy slash path with its turn-stopping and command handlers stubbed out."""
    from gateway.run import GatewayRunner
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._media_batch_delay_seconds = 60.0  # never flushes inside a test
    runner = object.__new__(GatewayRunner)
    runner._intake_adapter_for = lambda source: adapter
    runner._check_slash_access = lambda source, canonical_cmd: None

    async def _stop_owner_turn(_quick_key):
        return None

    async def _dispatch(event, cmd_def, quick_key, source):
        return "done"

    async def _status(event):
        return "status"

    runner._hm_stop_owner_turn = _stop_owner_turn
    runner._dispatch_busy_slash_command = _dispatch
    runner._handle_status_command = _status
    flushes = {chat_id: _buffer_photo(adapter, chat_id) for chat_id in ("42", "7")}
    try:
        yield runner, adapter, flushes
    finally:
        for task in [*flushes.values(), *adapter._pending_photo_batch_tasks.values()]:
            task.cancel()


def _buffered_chats(adapter) -> list[str]:
    return sorted(event.source.chat_id for event in adapter._pending_photo_batches.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["stop", "new", "reset"])
async def test_reset_command_during_a_running_turn_drops_the_chats_buffered_photo(rig, command):
    runner, adapter, flushes = rig
    event = _event(f"/{command}", "42")

    handled, _reply = await runner._hm_busy_slash_or_photo(event, event.source, "session-42")

    assert handled
    assert _buffered_chats(adapter) == ["7"]  # only this chat's burst goes
    assert flushes["42"].cancelling() and not flushes["7"].cancelling()


@pytest.mark.asyncio
async def test_status_during_a_running_turn_keeps_the_buffered_photo(rig):
    runner, adapter, _flushes = rig
    event = _event("/status", "42")

    assert await runner._hm_busy_slash_or_photo(event, event.source, "session-42") == (True, "status")

    assert _buffered_chats(adapter) == ["42", "7"]


@pytest.mark.asyncio
async def test_refused_stop_during_a_running_turn_keeps_the_buffered_photo(rig):
    runner, adapter, _flushes = rig
    runner._check_slash_access = lambda source, canonical_cmd: "not allowed"
    event = _event("/stop", "42")

    assert await runner._hm_busy_slash_or_photo(event, event.source, "session-42") == (True, "not allowed")

    assert _buffered_chats(adapter) == ["42", "7"]
