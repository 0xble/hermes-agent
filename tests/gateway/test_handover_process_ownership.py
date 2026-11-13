"""Handover cannot stop between a process exit, its watcher, and native delivery."""

import asyncio
import threading

import pytest

from gateway import zero_cut_handover_drain as drain
from tests.gateway.test_handover_owned_notices import owned_route, release_handover
from tests.gateway.test_wake_during_restart_drain_authority import start_human_turn
from tests.waiting import async_wait_until
from tools.process_registry import ProcessRegistry, ProcessSession


def owned_process(runner, source):
    route = runner.session_store._generate_session_key(source)
    return ProcessSession(
        id='proc_owned_handover', command='printf RESULT', session_key=route,
        parent_session_id=runner.session_store.peek_session_id(route), started_at=1.0,
        notify_on_complete=True, output_buffer='RESULT',
        watcher_platform='discord', watcher_chat_id='42', watcher_user_id='42', watcher_interval=1,
    )


@pytest.mark.asyncio
async def test_handover_waits_for_process_result_publication(monkeypatch):
    executed = []
    runner, adapter, authority, source = await owned_route(monkeypatch, executed)
    await start_human_turn(runner, adapter, authority, source, None)
    await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task))
    await async_wait_until(lambda: not adapter._active_sessions, what="initial adapter send to settle")
    registry = ProcessRegistry()
    monkeypatch.setattr('tools.process_registry.process_registry', registry)
    process = owned_process(runner, source)
    registry._running[process.id] = process
    publishing, release = threading.Event(), threading.Event()
    publish = registry._publish_finished

    def publish_after_barrier(session, was_running):
        publishing.set()
        assert release.wait(10), 'test must release process finalization'
        publish(session, was_running)

    monkeypatch.setattr(registry, '_publish_finished', publish_after_barrier)
    release_handover(runner)
    finishing = asyncio.create_task(asyncio.to_thread(registry._finish_exited, process, 0))
    try:
        assert await asyncio.to_thread(publishing.wait, 10)
        assert process.exited and registry.completion_queue.empty()
        assert drain.status(runner)['idle'] is False, 'exit is not publication of the result'
    finally:
        release.set()
        await finishing
    assert not registry.completion_queue.empty()
    assert drain.status(runner)['idle'] is False


@pytest.mark.asyncio
async def test_queue_drain_leaves_completion_owned_until_watcher_sends(monkeypatch):
    executed, sent = [], []
    runner, adapter, authority, source = await owned_route(monkeypatch, executed)
    await start_human_turn(runner, adapter, authority, source, None)
    await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task))
    await async_wait_until(lambda: not adapter._active_sessions, what="initial adapter send to settle")
    from gateway.platforms.base import SendResult

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SendResult(success=True, message_id=str(len(sent)))

    monkeypatch.setattr(adapter, 'send', send)
    registry = ProcessRegistry()
    monkeypatch.setattr('tools.process_registry.process_registry', registry)
    monkeypatch.setattr(runner, '_load_background_notifications_mode', lambda: 'off')
    runner._completion_notification_batch_window = 0
    process = owned_process(runner, source)
    registry._running[process.id] = process
    registry._finish_exited(process, 0)
    watcher = {
        'session_id': process.id, 'session_key': process.session_key,
        'parent_session_id': process.parent_session_id, 'platform': 'discord',
        'chat_id': '42', 'user_id': '42', 'chat_type': 'dm',
        'check_interval': .01, 'notify_on_complete': True,
    }
    release_handover(runner)
    await runner._drain_watch_notifications(registry.completion_queue)
    assert registry.completion_queue.empty(), 'ordinary completions belong to the process watcher'
    assert drain.status(runner)['idle'] is False, 'popping a completion is not its delivery'
    await asyncio.wait_for(runner._run_process_watcher(watcher), 10)
    await async_wait_until(lambda: drain.status(runner)['idle'], what='watcher and authority send to settle')
    assert len(executed) == 2 and 'RESULT' in executed[-1]
    assert len(sent) == 1 and 'RESULT' in sent[0]
    assert process._completion_delivered
    await asyncio.wait_for(runner._run_process_watcher(watcher), 10)
    assert len(executed) == 2 and len(sent) == 1
