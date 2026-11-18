"""Deferred post-turn bookkeeping keeps ownership until it finishes and never waits on cosmetics.

Replies are delivered before goal/loop bookkeeping. The loop watcher must still see the finished
turn's in-flight judge as an owner (else it skips the tick and loses LOOP_COMPLETE), and a slow
cosmetic post-delivery callback must not swallow the delivered signal on the native path.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import Mock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_results import execution_result
from hermes_cli import loops
from hermes_state_runtime import list_session_admissions
from tests.fakes.runtime_owner import initialize_session_authority
from tests.waiting import async_wait_until


async def _native_discord(tmp_path, monkeypatch, instance_id):
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False))
    adapter.gateway_runner, runner.adapters = runner, {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id=instance_id)
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', user_id='42', chat_type='dm')
    return runner, adapter, authority, source, runner._session_key_for_source(source)


async def _teardown(runner, adapter, authority):
    await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task),
                         return_exceptions=True)
    from gateway.session_runtime_workers import mutation_tasks
    while pending := mutation_tasks(authority):
        await asyncio.gather(*pending, return_exceptions=True)
    for task in list(adapter._background_tasks):
        task.cancel()
    authority.db.close()
    runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('interference', ['watcher_scan', 'stop_command'])
async def test_a_delivered_loop_tick_settles_despite_interference_during_its_judge(
        tmp_path, monkeypatch, interference):
    runner, adapter, authority, source, key = await _native_discord(tmp_path, monkeypatch, 'loop-race')
    turns, sent = [], []
    gate, entered = asyncio.Event(), asyncio.Event()

    async def send(chat_id, content, **kwargs):
        sent.append(content)
        return SendResult(success=True, message_id='sent')

    async def turn(event, *args, **kwargs):
        turns.append(event)
        text = 'seed reply' if len(turns) == 1 else 'LOOP_COMPLETE'
        execution_result.get()['result'] = {'final_response': text, 'messages': []}
        return text

    async def slow_goal_judge(**kwargs):
        if len(turns) >= 2:  # the aux judge of the tick's turn, still running after delivery
            entered.set()
            await gate.wait()

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    monkeypatch.setattr(runner, '_post_turn_goal_continuation', slow_goal_judge)
    monkeypatch.setattr('hermes_cli.goals.judge_goal', Mock(return_value=('done', 'x', None, None, None)))

    def admissions():
        owner = runner.session_store.peek_session_id(key)
        return list_session_admissions(authority.db, session_id=owner, pending_only=False) if owner else []

    try:
        await adapter.handle_message(MessageEvent(text='seed', source=source, message_id='seed'))
        await async_wait_until(
            lambda: len(admissions()) == 1 and all(r['status'] == 'terminal' for r in admissions())
            and not runner._is_session_running(key) and not adapter._background_tasks,
            timeout=20, what='seed turn settled')
        owner = runner.session_store.peek_session_id(key)
        await runner._warm_goals_session_db('loop race fixture')
        manager = loops.LoopManager(owner)
        manager.set('check work', interval_seconds=300, times=3, route={
            'platform': 'discord', 'chat_id': '42', 'chat_type': 'dm', 'user_id': '42',
        })
        manager.state.next_due_at = 0
        manager._save()
        await runner._loop_wakeup_fire_one(owner, manager.state, 1e12, set())
        await asyncio.wait_for(entered.wait(), 20)
        # The tick's reply is delivered and its turn settled; the judge is mid-flight. A watcher scan
        # must treat that judge as the tick's owner, and a /stop must not drop the tick's verdict.
        if interference == 'watcher_scan':
            await runner._loop_wakeup_fire_one(owner, loops.load_loop(owner), time.time(), set())
        else:
            runner._invalidate_session_run_generation(key, reason='stop_command')
        gate.set()
        await async_wait_until(
            lambda: not adapter._background_tasks and not runner._is_session_running(key)
            and all(r['status'] == 'terminal' for r in admissions()),
            timeout=20, what='deferred bookkeeping finished')
        await runner._loop_wakeup_fire_one(owner, loops.load_loop(owner), time.time(), set())
        assert loops.load_loop(owner).status == 'done'
    finally:
        gate.set()
        await _teardown(runner, adapter, authority)


@pytest.mark.asyncio
async def test_hung_cosmetic_cleanup_does_not_strand_deferred_bookkeeping(tmp_path, monkeypatch):
    import gateway.platforms.base as base

    monkeypatch.setattr(base, '_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS', 0.2)
    runner, adapter, authority, source, key = await _native_discord(tmp_path, monkeypatch, 'cleanup-race')
    hooks, sent = [], []
    never = asyncio.Event()

    async def send(chat_id, content, **kwargs):
        sent.append(content)
        return SendResult(success=True, message_id='sent')

    async def hung_cleanup():
        await never.wait()  # e.g. bubble deletes stuck behind a platform flood limit

    async def turn(event, src, quick_key, generation, *args, **kwargs):
        captured = execution_result.get()
        captured['result'] = {'final_response': 'hi', 'messages': []}
        adapter.register_post_delivery_callback(quick_key, hung_cleanup, generation=generation)
        captured['post_delivery'] = (adapter, quick_key, generation)
        return 'hi'

    async def spy(**kwargs):
        hooks.append(kwargs)

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    monkeypatch.setattr(runner, '_run_post_turn_hooks', spy)
    try:
        await adapter.handle_message(MessageEvent(text='hello', source=source, message_id='m1'))
        await async_wait_until(lambda: sent and hooks and not adapter._background_tasks,
                               timeout=10, what='bookkeeping after a hung cleanup')
        assert sent == ['hi'] and len(hooks) == 1
    finally:
        never.set()
        await _teardown(runner, adapter, authority)
