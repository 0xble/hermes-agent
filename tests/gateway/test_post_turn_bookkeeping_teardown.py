"""Delivery callbacks must never strand a session's bookkeeping or progress cleanup."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.session_results import execution_result
from tests.gateway.test_busy_burst_serial_reader import _signal
from tests.gateway.test_duplicate_user_message import _bootstrap
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_path', ['superseded', 'refused', 'missed_callback'])
async def test_drained_turn_releases_guard_and_runs_bookkeeping_once(monkeypatch, tmp_path, caplog, exit_path):
    from gateway import session_ingress

    adapter, source = _signal(monkeypatch)
    runner = _bootstrap(monkeypatch, tmp_path)
    runner.adapters = {source.platform: adapter}
    runner._begin_session_run_generation = lambda _key: 5
    hook_tasks = []
    hooks = AsyncMock(side_effect=lambda **kwargs: hook_tasks.append(asyncio.current_task()))
    newer_cleanup = AsyncMock()
    key = runner._session_key_for_source(source)
    guard = asyncio.Event()
    guard._hermes_run_generation = 4
    adapter._active_sessions[key] = guard

    async def turn(event, source, key, generation):
        if exit_path == 'superseded':
            # The real handler returns before binding when a queued loop wake lost its fence.
            return await type(runner)._handle_message_with_agent(runner, event, source, key, generation)
        runner._bind_adapter_run_generation(adapter, key, generation)
        if exit_path == 'refused':
            adapter.register_post_delivery_callback(key, newer_cleanup, generation=6)
        return None

    monkeypatch.setattr(runner, '_prepare_loop_wake_execution', AsyncMock(return_value=False))
    monkeypatch.setattr(runner, '_hm_dispatch_idle_commands', AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    monkeypatch.setattr(runner, '_run_post_turn_hooks', hooks)
    monkeypatch.setattr(adapter, 'send', AsyncMock(return_value=SendResult(success=True)))
    monkeypatch.setattr(adapter, 'send_typing', lambda *a, **k: asyncio.sleep(0))
    if exit_path == 'missed_callback':
        monkeypatch.setattr(adapter, '_fire_post_delivery_callback', AsyncMock())
        monkeypatch.setattr(session_ingress, '_BOOKKEEPING_JOIN_TIMEOUT_SECONDS', 0.05, raising=False)
    adapter.set_message_handler(runner._handle_message)
    event = MessageEvent(text='queued wake', source=source, message_id='queued')
    assert adapter._spawn_drain_task(event, key)
    task = adapter._session_tasks[key]
    try:
        await asyncio.wait_for(asyncio.shield(task), 5)
        hooks.assert_awaited_once()
        assert hook_tasks == [task], 'unreachable delivery must finish bookkeeping inline'
        assert key not in adapter._active_sessions
        assert key not in adapter._session_tasks
        if exit_path == 'superseded':
            runner._prepare_loop_wake_execution.assert_awaited_once()
        if exit_path == 'missed_callback':
            assert 'Post-turn bookkeeping join timed out' in caplog.text
        # A late delivery signal cannot repeat the timeout's inline bookkeeping.
        await type(adapter)._fire_post_delivery_callback(adapter, key, 5)
        await async_wait_until(lambda: not adapter._background_tasks, timeout=5, what='bookkeeping tasks')
        hooks.assert_awaited_once()
        newer_cleanup.assert_not_awaited()
    finally:
        tasks = {task, *adapter._background_tasks}
        for pending in tasks:
            if not pending.done():
                pending.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('different_owner', [False, True])
@pytest.mark.parametrize('delivery', ['native', 'detached'])
async def test_fifo_fires_cleanup_and_bookkeeping_owners(monkeypatch, tmp_path, different_owner, delivery):
    from gateway.session_ingress import _claim, _fire_post_delivery, fire_turn_post_delivery, join_turn_bookkeeping

    adapter, source = _signal(monkeypatch)
    runner = _bootstrap(monkeypatch, tmp_path)
    runner.adapters = {source.platform: adapter}
    cleanup_adapter = _signal(monkeypatch)[0] if different_owner else adapter
    key = runner._session_key_for_source(source)
    cleanup_generation = 4 if different_owner else 5
    cleanup, hooks = AsyncMock(), AsyncMock()
    cleanup_adapter.register_post_delivery_callback(key, cleanup, generation=cleanup_generation)
    captured = {'post_delivery_owned': True, 'post_delivery': (cleanup_adapter, key, cleanup_generation)}
    event = MessageEvent(text='answer', source=source)
    monkeypatch.setattr(runner, '_run_post_turn_hooks', hooks)
    token = execution_result.set(captured)
    try:
        await runner._defer_post_turn_hooks(
            agent_result='ok', source=source, is_internal=False, event=event, session_key=key, generation=5)
    finally:
        execution_result.reset(token)
    event._turn_post_delivery = captured['post_delivery']
    _claim(captured['post_delivery'])
    if delivery == 'native':
        await fire_turn_post_delivery(event)
    else:
        _fire_post_delivery(captured['post_delivery'])
    await join_turn_bookkeeping(event)
    cleanup.assert_awaited_once()
    hooks.assert_awaited_once()
    assert not cleanup_adapter._post_delivery_callbacks_by_generation
    assert not adapter._post_delivery_callbacks_by_generation
