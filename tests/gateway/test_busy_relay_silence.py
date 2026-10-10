"""Busy-session relays retain their intentional-silence contract."""

import asyncio

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


class BusyAdapter(BasePlatformAdapter):
    def __init__(self, *, busy_text_mode: str):
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
        self._busy_text_mode = busy_text_mode

    @property
    def name(self):
        return "telegram"

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "private"}


async def _unused_message_handler(event):
    raise AssertionError("busy event must not start a concurrent turn")


async def _not_handled(event, session_key):
    return False


def _relay_event(source, *, receipt="r1"):
    return MessageEvent(
        text=f"[relay from=agent@example.com receipt={receipt}]\nplease handle this",
        source=source,
        message_id=receipt,
    )


def _busy_adapter(mode):
    adapter = BusyAdapter(busy_text_mode=mode)
    adapter.set_message_handler(_unused_message_handler)
    adapter.set_busy_session_handler(_not_handled)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="u1")
    key = adapter._event_session_key(_relay_event(source))
    adapter._active_sessions[key] = asyncio.Event()
    return adapter, source, key


@pytest.mark.asyncio
async def test_busy_queue_debounce_marks_relay_unaddressed_before_buffering():
    adapter, source, key = _busy_adapter("queue")
    event = _relay_event(source)

    await adapter.handle_message(event)

    assert adapter._text_debounce[key].event.reply_expected is False
    adapter._discard_text_debounce(key)


@pytest.mark.asyncio
async def test_busy_non_debounce_merge_keeps_relay_unaddressed():
    adapter, source, key = _busy_adapter("interrupt")

    await adapter.handle_message(_relay_event(source, receipt="r1"))
    await adapter.handle_message(_relay_event(source, receipt="r2"))

    assert adapter._pending_messages[key].reply_expected is False


@pytest.mark.asyncio
async def test_busy_relay_after_an_existing_relay_does_not_reset_false():
    adapter, source, key = _busy_adapter("queue")

    first = _relay_event(source, receipt="r1")
    first.reply_expected = False
    adapter._pending_messages[key] = first
    await adapter.handle_message(_relay_event(source, receipt="r2"))
    await adapter._flush_text_debounce_now(key)

    assert adapter._pending_messages[key].reply_expected is False


@pytest.mark.asyncio
async def test_busy_merge_with_typed_human_keeps_the_silence_guard():
    adapter, source, key = _busy_adapter("interrupt")

    await adapter.handle_message(_relay_event(source))
    await adapter.handle_message(MessageEvent(text="typed follow-up", source=source, message_id="human-1"))

    assert adapter._pending_messages[key].reply_expected is None

