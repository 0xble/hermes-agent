"""Restore admission wakeups survive reconnect ownership and no-progress scans."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageEvent
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.mark.asyncio
async def test_live_input_during_reconnect_tail_drains_after_owner_release(monkeypatch):
    runner, adapter = make_restart_runner()
    runner._startup_restore_in_progress = False
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    slow_source = make_restart_source(chat_id="other")
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    live_dispatched = asyncio.Event()
    accepted = []
    runner._normalize_source_for_session_key = lambda value: value
    runner._resume_pending_candidates = lambda **_kwargs: []
    runner._schedule_resume_pending_sessions = lambda **_kwargs: None
    runner._queue_startup_restore_event(MessageEvent(text="Y-slow", source=slow_source))

    def recover(owner, **_kwargs):
        owner._queue_startup_restore_event(MessageEvent(text="A-queued", source=source))

    monkeypatch.setattr("gateway.run_pending_recovery.recover_pending_shutdown_flush", recover)

    async def handle(event):
        accepted.append(event.text)
        event._gateway_accepted = True
        if event.text == "Y-slow":
            slow_started.set()
            await release_slow.wait()
        elif event.text == "B-live":
            live_dispatched.set()

    adapter.handle_message = handle
    recovery = asyncio.create_task(runner._recover_spool_after_reconnect(source.platform))
    try:
        await asyncio.wait_for(slow_started.wait(), 10)
        # The reconnect batch has scanned its queue and is waiting on this unrelated tail.
        assert accepted == ["A-queued", "Y-slow"]
        assert runner._reconnect_restore_keys == {key: 1}
        assert not recovery.done()
        live = MessageEvent(text="B-live", source=source, user_id="u1")
        assert await runner._hm_admit_event(live) is None
        assert runner._startup_restore_queue == [live]

        release_slow.set()
        await asyncio.wait_for(recovery, 10)
        await asyncio.wait_for(live_dispatched.wait(), 10)
        await asyncio.wait_for(runner._startup_restore_drain_task, 10)
        assert accepted == ["A-queued", "Y-slow", "B-live"]
        assert runner._startup_restore_queue == []
        assert runner._startup_restore_keys == {}
        assert runner._reconnect_restore_keys == {}
    finally:
        release_slow.set()
        if not recovery.done():
            recovery.cancel()
        await asyncio.gather(recovery, return_exceptions=True)
        await runner._get_replay_scheduler().close()


@pytest.mark.asyncio
async def test_restore_release_after_no_progress_scan_requests_another_pass():
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    queued = MessageEvent(text="waiting input", source=source)
    runner._queue_startup_restore_event(queued, session_key=key)
    runner._startup_restore_keys[key] += 1  # The older dispatch still owns its claim.
    runner._startup_restore_claimed_keys = {key}
    runner._normalize_source_for_session_key = lambda value: value
    scanned = asyncio.Event()
    finish_pass = asyncio.Event()
    real_drain = runner._drain_startup_restore_queue
    calls = 0

    async def drain(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = await real_drain(*args, **kwargs)
        if calls == 1:
            assert result == 0
            scanned.set()
            await finish_pass.wait()
        return result

    async def handle(event):
        event._gateway_accepted = True

    adapter.handle_message = AsyncMock(side_effect=handle)
    runner._drain_startup_restore_queue = drain
    runner._schedule_startup_restore_drain()
    task = runner._startup_restore_drain_task
    try:
        await asyncio.wait_for(scanned.wait(), 10)
        runner._release_startup_restore_key(key)
        assert runner._startup_restore_drain_task is task
        finish_pass.set()
        await asyncio.wait_for(task, 10)
        adapter.handle_message.assert_awaited_once_with(queued)
        assert runner._startup_restore_queue == []
        assert runner._startup_restore_keys == {}
        assert calls >= 2
    finally:
        finish_pass.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runner._get_replay_scheduler().close()


@pytest.mark.asyncio
async def test_startup_fence_opening_drains_input_arriving_after_initial_scan():
    runner, adapter = make_restart_runner()
    runner._startup_restore_in_progress = True
    source = make_restart_source()
    runner._normalize_source_for_session_key = lambda value: value
    scanned = asyncio.Event()
    finish_scan = asyncio.Event()
    dispatched = asyncio.Event()
    real_drain = runner._drain_startup_restore_queue

    async def drain(*args, **kwargs):
        result = await real_drain(*args, **kwargs)
        if kwargs.get("wait") is False:
            scanned.set()
            await finish_scan.wait()
        return result

    async def handle(event):
        event._gateway_accepted = True
        dispatched.set()

    runner._drain_startup_restore_queue = drain
    adapter.handle_message = AsyncMock(side_effect=handle)
    startup = asyncio.create_task(runner._finish_startup_restore())
    try:
        await asyncio.wait_for(scanned.wait(), 10)
        live = MessageEvent(text="late startup input", source=source, user_id="u1")
        assert await runner._hm_admit_event(live) is None
        assert runner._startup_restore_queue == [live]
        finish_scan.set()
        await asyncio.wait_for(startup, 10)
        await asyncio.wait_for(dispatched.wait(), 10)
        await asyncio.wait_for(runner._startup_restore_drain_task, 10)
        adapter.handle_message.assert_awaited_once_with(live)
        assert runner._startup_restore_keys == {}
        assert runner._startup_restore_queue == []
    finally:
        finish_scan.set()
        if not startup.done():
            startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        await runner._get_replay_scheduler().close()
