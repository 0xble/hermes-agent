"""Per-adapter typing refresh cadence tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import BasePlatformAdapter, Platform, PlatformConfig


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
async def test_slow_send_anchors_deadline_after_send(monkeypatch):
    """A 1.2s typing send: the next tick waits a full interval after the send returns."""
    from gateway.platforms import base as base_module

    expected = 5.7
    adapter = _StubAdapter(PlatformConfig(enabled=True), Platform.DISCORD)
    adapter.stop_typing = AsyncMock()
    stop_event = asyncio.Event()
    clock = [0.0]
    started_at = []
    real_sleep = asyncio.sleep

    class _Clock:
        def time(self):
            return clock[0]

    async def capture_sleep(delay):
        clock[0] += delay
        await real_sleep(0)

    async def slow_typing(chat_id, metadata=None):
        started_at.append(clock[0])
        clock[0] += 1.2  # well inside the 1.5s send timeout, so not a timed-out tick
        if len(started_at) == 2:
            stop_event.set()
        return None

    adapter.send_typing = slow_typing
    monkeypatch.setattr(base_module.asyncio, "get_running_loop", lambda: _Clock())
    monkeypatch.setattr(base_module.asyncio, "sleep", capture_sleep)

    await adapter._keep_typing("123", interval=4.5, stop_event=stop_event)

    assert started_at == [pytest.approx(0.0), pytest.approx(expected)]
