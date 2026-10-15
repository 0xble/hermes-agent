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
    from gateway.platforms import base as base_module
    from plugins.platforms.telegram.adapter import _TYPING_SKIPPED_LOCK_BUSY, _TYPING_TICK_RETRY_SECS

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter.stop_typing = AsyncMock()
    stop_event = asyncio.Event()
    clock = [0.0]
    sent_at = []
    sleep_delays = []
    real_sleep = asyncio.sleep

    class _Clock:
        def time(self):
            return clock[0]

    async def capture_sleep(delay):
        sleep_delays.append(delay)
        clock[0] += delay
        await real_sleep(0)

    async def typing(chat_id, metadata=None):
        sent_at.append(clock[0])
        if len(sent_at) == 1:
            return _TYPING_SKIPPED_LOCK_BUSY
        stop_event.set()
        return None

    adapter.send_typing = typing
    monkeypatch.setattr(base_module.asyncio, "get_running_loop", lambda: _Clock())
    monkeypatch.setattr(base_module.asyncio, "sleep", capture_sleep)

    await adapter._keep_typing("123", interval=1.0, stop_event=stop_event)

    # No retry fits, so the only second send is the regular tick, exactly one interval later.
    assert sent_at == [pytest.approx(0.0), pytest.approx(1.0)]
    assert not any(delay == pytest.approx(_TYPING_TICK_RETRY_SECS) for delay in sleep_delays)
    assert sum(sleep_delays) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_telegram_lock_busy_tick_returns_sentinel_and_retries_through_real_lock(monkeypatch):
    """A delivery holding the per-chat send lock makes the real send_typing return the lock-busy
    sentinel; _keep_typing retries once, and the retry sends once the lock is free."""
    from gateway.platforms import base as base_module
    from plugins.platforms.telegram.adapter import _TYPING_SKIPPED_LOCK_BUSY, _TYPING_TICK_RETRY_SECS

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter.stop_typing = AsyncMock()
    adapter._bot = AsyncMock()
    stop_event = asyncio.Event()
    clock = [0.0]
    sleep_delays = []
    results = []
    action_at = []
    real_sleep = asyncio.sleep
    real_send_typing = adapter.send_typing

    # Another task (a real delivery) owns the chat's send lock.
    lock = asyncio.Lock()
    await lock.acquire()
    adapter.__dict__.setdefault("_telegram_chat_send_locks", {})["123"] = lock
    adapter.__dict__.setdefault("_telegram_chat_send_lock_owners", {})["123"] = object()

    class _Clock:
        def time(self):
            return clock[0]

    async def capture_sleep(delay):
        sleep_delays.append(delay)
        clock[0] += delay
        if delay == pytest.approx(_TYPING_TICK_RETRY_SECS) and lock.locked():
            # The delivery finishes during the retry delay.
            adapter._telegram_chat_send_lock_owners.pop("123", None)
            lock.release()
        await real_sleep(0)

    async def chat_action(**kwargs):
        action_at.append(clock[0])
        stop_event.set()

    async def typing(chat_id, metadata=None):
        result = await real_send_typing(chat_id, metadata=metadata)
        results.append(result)
        return result

    adapter._bot.send_chat_action = AsyncMock(side_effect=chat_action)
    adapter.send_typing = typing
    monkeypatch.setattr(adapter, "_chat_budget", lambda: SimpleNamespace(
        typing_blocked=lambda key: False, try_take=lambda key, kind: True))
    monkeypatch.setattr(adapter, "_daily_sheds", lambda key, kind: False)
    monkeypatch.setattr(adapter, "_bot_meters_chat_budget", lambda: True)
    monkeypatch.setattr(base_module.asyncio, "get_running_loop", lambda: _Clock())
    monkeypatch.setattr(base_module.asyncio, "sleep", capture_sleep)

    await adapter._keep_typing("123", interval=adapter.typing_refresh_interval, stop_event=stop_event)

    assert results == [_TYPING_SKIPPED_LOCK_BUSY, None]
    assert sleep_delays[0] == pytest.approx(_TYPING_TICK_RETRY_SECS)
    # Exactly one Bot API call, made by the retry inside the first tick.
    assert action_at == [pytest.approx(_TYPING_TICK_RETRY_SECS)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("adapter_kind", "expected"), [("telegram", 4.5), ("base", 5.7)])
async def test_slow_send_anchors_deadline_by_adapter(monkeypatch, adapter_kind, expected):
    """A 1.2s sendChatAction: Telegram keeps its tick-start cadence; other adapters stay post-send."""
    from gateway.platforms import base as base_module

    if adapter_kind == "telegram":
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    else:
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


@pytest.mark.asyncio
async def test_telegram_keep_typing_sends_nothing_and_never_retries_during_flood_control(monkeypatch):
    """Active flood control fails closed: no sendChatAction, and the shed tick is not retried."""
    from gateway.platforms import base as base_module
    from plugins.platforms.telegram.adapter import _TYPING_TICK_RETRY_SECS

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter.stop_typing = AsyncMock()
    adapter._bot = AsyncMock()
    adapter._bot.send_chat_action = AsyncMock(return_value=None)
    stop_event = asyncio.Event()
    clock = [0.0]
    sleep_delays = []
    real_sleep = asyncio.sleep

    class _Clock:
        def time(self):
            return clock[0]

    async def capture_sleep(delay):
        sleep_delays.append(delay)
        clock[0] += delay
        if clock[0] >= 3 * adapter.typing_refresh_interval:
            stop_event.set()
        await real_sleep(0)

    monkeypatch.setattr(adapter, "_send_flood_cooldown_remaining", lambda chat_id: 60.0)
    monkeypatch.setattr(adapter, "_chat_budget", lambda: SimpleNamespace(
        typing_blocked=lambda key: False, try_take=lambda key, kind: True))
    monkeypatch.setattr(adapter, "_daily_sheds", lambda key, kind: False)
    monkeypatch.setattr(base_module.asyncio, "get_running_loop", lambda: _Clock())
    monkeypatch.setattr(base_module.asyncio, "sleep", capture_sleep)

    await adapter._keep_typing("123", interval=adapter.typing_refresh_interval, stop_event=stop_event)

    adapter._bot.send_chat_action.assert_not_called()
    assert not any(delay == pytest.approx(_TYPING_TICK_RETRY_SECS) for delay in sleep_delays)
