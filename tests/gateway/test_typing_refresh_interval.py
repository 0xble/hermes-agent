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

    assert len(timeouts) == 1
    assert 0 < timeouts[0] < adapter.typing_refresh_interval


@pytest.mark.asyncio
async def test_base_keep_typing_deadline_starts_after_send_returns(monkeypatch):
    """The base adapter retains its historical post-send cadence."""
    import gateway.platforms.base as base_module

    adapter = _StubAdapter(PlatformConfig(enabled=True), Platform.DISCORD)
    adapter.stop_typing = AsyncMock()
    sleep_delays = []
    clock = [0.0]

    class _Clock:
        def time(self):
            return clock[0]

    class _Finished(Exception):
        pass

    async def capture_wait_for(awaitable, *, timeout):
        result = await awaitable
        clock[0] += 0.4
        return result

    async def capture_sleep(delay):
        sleep_delays.append(delay)
        raise _Finished

    monkeypatch.setattr(base_module.asyncio, "get_running_loop", lambda: _Clock())
    monkeypatch.setattr(base_module.asyncio, "wait_for", capture_wait_for)
    monkeypatch.setattr(base_module.asyncio, "sleep", capture_sleep)

    with pytest.raises(_Finished):
        await adapter._keep_typing("123", interval=2.0)

    assert sleep_delays == [pytest.approx(2.0)]


@pytest.mark.asyncio
async def test_telegram_keep_typing_retries_lock_skipped_tick_promptly(monkeypatch):
    """A skipped tick is retried at the hook delay instead of the next 4.5s deadline."""
    from gateway.platforms import base as base_module
    from plugins.platforms.telegram.adapter import _TYPING_SKIPPED_LOCK_BUSY, _TYPING_TICK_RETRY_SECS

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter.stop_typing = AsyncMock()
    stop_event = asyncio.Event()
    clock = [0.0]
    sleep_delays = []
    attempts = []
    real_sleep = asyncio.sleep

    class _Clock:
        def time(self):
            return clock[0]

    async def capture_sleep(delay):
        sleep_delays.append(delay)
        clock[0] += delay
        await real_sleep(0)

    async def typing(chat_id, metadata=None):
        attempts.append(clock[0])
        if len(attempts) == 2:
            return _TYPING_SKIPPED_LOCK_BUSY
        if len(attempts) == 3:
            stop_event.set()
        return None

    adapter.send_typing = typing
    monkeypatch.setattr(base_module.asyncio, "get_running_loop", lambda: _Clock())
    monkeypatch.setattr(base_module.asyncio, "sleep", capture_sleep)

    await adapter._keep_typing("123", interval=adapter.typing_refresh_interval, stop_event=stop_event)

    assert attempts == [pytest.approx(0.0), pytest.approx(4.5), pytest.approx(5.0)]
    assert any(delay == pytest.approx(_TYPING_TICK_RETRY_SECS) for delay in sleep_delays)
    assert attempts[-1] < 9.0


@pytest.mark.asyncio
async def test_telegram_keep_typing_skips_retry_without_room_before_next_tick(monkeypatch):
    """At a 1s interval a 0.5s retry plus a 0.75s send cannot fit, so the next regular tick covers it."""
    from plugins.platforms.telegram.adapter import _TYPING_SKIPPED_LOCK_BUSY, _TYPING_TICK_RETRY_SECS

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter.stop_typing = AsyncMock()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    sent_at = []
    sleep_delays = []
    real_sleep = asyncio.sleep

    async def capture_sleep(delay):
        sleep_delays.append(delay)
        await real_sleep(delay)

    monkeypatch.setattr("gateway.platforms.base.asyncio.sleep", capture_sleep)

    async def typing(chat_id, metadata=None):
        sent_at.append(loop.time())
        if len(sent_at) == 1:
            return _TYPING_SKIPPED_LOCK_BUSY
        stop_event.set()
        return None

    adapter.send_typing = typing
    await asyncio.wait_for(adapter._keep_typing("123", interval=1.0, stop_event=stop_event), timeout=3)
    monkeypatch.undo()

    assert len(sent_at) == 2
    assert not any(delay == pytest.approx(_TYPING_TICK_RETRY_SECS) for delay in sleep_delays)
