"""Per-adapter typing refresh cadence tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import BasePlatformAdapter, Platform, PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


class _StubAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


def _event(chat_id="123"):
    return SimpleNamespace(source=SimpleNamespace(chat_id=chat_id))


@pytest.mark.asyncio
async def test_start_typing_refresh_passes_base_interval_to_keep_typing():
    adapter = _StubAdapter(PlatformConfig(enabled=True), Platform.DISCORD)
    adapter._keep_typing = AsyncMock()
    interrupt_event = asyncio.Event()

    task = adapter._start_typing_refresh(_event(), interrupt_event, metadata={"thread_id": "t"})
    await task

    adapter._keep_typing.assert_awaited_once_with(
        "123", interval=2.0, metadata={"thread_id": "t"}
    )


@pytest.mark.asyncio
async def test_telegram_typing_refresh_uses_4500ms_interval():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._keep_typing = AsyncMock()
    interrupt_event = asyncio.Event()

    task = adapter._start_typing_refresh(_event(), interrupt_event, metadata=None)
    await task

    adapter._keep_typing.assert_awaited_once_with(
        "123", interval=4.5, metadata=None
    )


@pytest.mark.parametrize(
    ("extra_value", "expected"),
    [("3.25", 3.25), ("0.5", 1.0), ("99", 4.75), ("not-a-number", 4.5)],
)
def test_telegram_typing_refresh_extra_is_honored_and_bounded(extra_value, expected):
    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"typing_refresh_interval_seconds": extra_value},
        )
    )

    assert adapter.typing_refresh_interval == pytest.approx(expected)


@pytest.mark.asyncio
async def test_keep_typing_send_timeout_stays_inside_telegram_interval(monkeypatch):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter.stop_typing = AsyncMock()
    adapter.send_typing = AsyncMock()
    stop_event = asyncio.Event()
    timeouts = []

    async def capture_wait_for(awaitable, *, timeout):
        timeouts.append(timeout)
        result = await awaitable
        stop_event.set()
        return result

    monkeypatch.setattr("gateway.platforms.base.asyncio.wait_for", capture_wait_for)
    await adapter._keep_typing("123", interval=adapter.typing_refresh_interval, stop_event=stop_event)

    assert timeouts == [1.5]
    assert timeouts[0] < adapter.typing_refresh_interval
