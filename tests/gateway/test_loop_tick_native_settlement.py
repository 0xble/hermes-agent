"""A scheduler-stamped tick must survive native FIFO admission and settle from its own reply."""
import asyncio
from unittest.mock import Mock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from tests.fakes.runtime_owner import initialize_session_authority
from gateway.session_results import execution_result
from hermes_cli import loops
from hermes_state_runtime import list_session_admissions
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['LOOP_COMPLETE', 'NO_REPLY', '[SILENT]', 'condition met'])
async def test_stamped_loop_reply_settles_through_native_admission(tmp_path, monkeypatch, reply):
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False))
    adapter.gateway_runner, runner.adapters = runner, {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='loop-settle')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', user_id='42', chat_type='dm')
    key = runner._session_key_for_source(source)
    turns, hooks, sent = [], [], []
    complete = runner._post_turn_loop_completion

    async def send(chat_id, content, **kwargs):
        sent.append(content)
        return SendResult(success=True, message_id='sent')

    async def turn(event, *args, **kwargs):
        turns.append(event)
        text = 'seed reply' if len(turns) == 1 else reply
        execution_result.get()['result'] = {'final_response': text, 'messages': []}
        return text

    async def capture_completion(**kwargs):
        hooks.append((kwargs['loop_tick'], kwargs['final_response']))
        await complete(**kwargs)

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    monkeypatch.setattr(runner, '_post_turn_loop_completion', capture_completion)
    judge = Mock(return_value=('done', 'condition met', None, None, None))
    monkeypatch.setattr('hermes_cli.goals.judge_goal', judge)

    def admissions():
        owner = runner.session_store.peek_session_id(key)
        return list_session_admissions(authority.db, session_id=owner, pending_only=False) if owner else []

    async def idle(count):
        await async_wait_until(
            lambda: len(admissions()) == count and all(row['status'] == 'terminal' for row in admissions())
            and not runner._is_session_running(key) and not adapter._background_tasks,
            timeout=20, what='native admission and delivery to settle',
        )

    try:
        # A real committed human route makes the scheduler take admit_route_wake, not a
        # direct adapter double. Only inference and the external --until judge are replaced.
        await adapter.handle_message(MessageEvent(text='seed', source=source, message_id='seed'))
        await idle(1)
        owner = runner.session_store.peek_session_id(key)
        await runner._warm_goals_session_db('loop settlement fixture')
        manager = loops.LoopManager(owner)
        manager.set('check disposable work', interval_seconds=300, times=3, until='condition met', route={
            'platform': 'discord', 'chat_id': '42', 'chat_type': 'dm', 'user_id': '42',
        })
        manager.state.next_due_at = 0
        manager._save()
        await runner._loop_wakeup_fire_one(owner, manager.state, 1e12, set())
        await idle(2)
        state = loops.load_loop(owner)
        tick = (state.created_at, 1)
        print(f'HOOK reply={reply!r} observed={hooks[-1]!r} expected_tick={tick!r}')
        assert len(turns) == 2
        assert hooks[-1] == (tick, reply)
        assert turns[-1].reply_expected is False
        assert turns[-1].inherit_channel_pin is True
        assert not state.awaiting_response, 'completed turn was left for recover_tick'
        assert state.ticks_fired == 1
        assert state.status == ('done' if reply in ('LOOP_COMPLETE', 'condition met') else 'active')
        if reply in ('NO_REPLY', '[SILENT]'):
            assert state.next_due_at > state.last_fired_at
            judge.assert_not_called()
            assert reply not in sent
        elif reply == 'condition met':
            judge.assert_called_once()
        else:
            judge.assert_not_called()
    finally:
        await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task),
                             return_exceptions=True)
        from gateway.session_runtime_workers import mutation_tasks
        while pending := mutation_tasks(authority):
            await asyncio.gather(*pending, return_exceptions=True)
        authority.db.close()
        runner.session_store.close_all_db_handles()
