"""ACP ``/retry`` on the gateway path: real authority, SQLite and ``AuthorityConnection.dispatch``
behind ``GatewayACPAgent.prompt`` (#140190 review).

An image turn's refusal is the command's reply, not a failed request; a rewind whose snapshot
refresh lost its connection is replayed by the next ``/retry``, never re-derived from the
already-shortened transcript (which would remove the turn before it too)."""
import asyncio
import contextlib
from types import SimpleNamespace

import acp
import pytest

from acp_adapter.gateway_server import GatewayACPAgent
from gateway.config import GatewayConfig
from gateway.session import SessionStore
from tests.fakes.runtime_owner import initialize_session_authority
from gateway.session_controls import AuthorityConnection
from hermes_cli.gateway_client import GatewayClientError
from tests.acp_adapter.test_gateway_unacked_slash import SlashWire

IMAGE = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}


class LossyResume(SlashWire):
    """Loses the ``session.resume`` right after a committed rewind (a dropped connection)."""

    def __init__(self, loop):
        super().__init__(loop)
        self.lose_ack, self.after_rewind, self.lost = False, False, 0

    async def rpc(self, method, _timeout=None, **params):
        if method == 'session.resume' and self.after_rewind and not self.lost:
            self.lost += 1
            raise GatewayClientError('Gateway disconnected')
        result = await super().rpc(method, **params)
        self.after_rewind = method == 'session.mutate' and params.get('operation') == 'rewind'
        return result


@contextlib.asynccontextmanager
async def _editor(tmp_path, monkeypatch, wire_class=SlashWire, *, persist_answer=False):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    executed = []

    async def answer(event):
        from gateway.session_results import execution_result
        executed.append(event.text)
        if persist_answer:
            store._db.append_message(sid, 'user', event.text)
            store._db.append_message(sid, 'assistant', 'ACK_' + event.text)
        execution_result.get()['result'] = {'final_response': 'ACK_' + event.text, 'completed': True, 'messages': []}
        return 'ACK_' + event.text

    from gateway import run
    from gateway.session_local import create_local_session
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'acp': []}})
    runner = SimpleNamespace(_session_db=store._db, session_store=store, _draining=False, adapters={},
                             _handle_message=answer, _adapter_for_source=lambda source: None,
                             _cached_agent_for=lambda route: None, _evict_cached_agent=lambda route: None,
                             _resolve_session_agent_runtime=lambda **kwargs: ('frozen', {}))
    authority = await initialize_session_authority(runner, profile_id='p', instance_id='owner')
    wire = wire_class(asyncio.get_running_loop())
    wire.lose_ack = False
    wire.viewer = AuthorityConnection(authority, wire, {'user_id': 'editor', 'profile_id': 'p'})
    sid = create_local_session(authority, wire.viewer.actor, {'request_id': 'editor', 'source': 'acp',
                                                             'cwd': str(tmp_path), 'model': 'frozen',
                                                             'toolsets': []}).session_id
    agent = GatewayACPAgent()
    agent._gateway = wire
    updates = []

    async def session_update(session_id, update):
        updates.append(update.content.text)
    agent._conn = SimpleNamespace(session_update=session_update)
    agent._snapshots[sid] = await wire.rpc('session.resume', session_id=sid)
    agent._event_task = asyncio.create_task(agent._events())
    try:
        yield SimpleNamespace(agent=agent, db=authority.db, sid=sid, wire=wire, updates=updates, executed=executed,
                              authority=authority)
    finally:
        agent._event_task.cancel()
        await wire.viewer.close()
        store.close_all_db_handles()


def _users(db, sid):
    return [m['content'] for m in db.get_messages(sid) if m['role'] == 'user']


@pytest.mark.asyncio
async def test_retry_of_an_image_turn_is_the_commands_reply_and_keeps_history(tmp_path, monkeypatch):
    async with _editor(tmp_path, monkeypatch) as e:
        e.db.append_message(e.sid, 'user', [{'type': 'text', 'text': 'what is this?'}, IMAGE])
        e.db.append_message(e.sid, 'assistant', 'a picture')
        response = await asyncio.wait_for(e.agent.prompt([acp.text_block('/retry')], e.sid), 5)
        assert response.stop_reason == 'end_turn'
        assert any(u.startswith('/retry: retry does not support media') for u in e.updates), e.updates
        assert len(e.db.get_messages(e.sid)) == 2 and not e.executed


@pytest.mark.asyncio
async def test_retry_after_a_lost_snapshot_refresh_replays_the_same_rewind(tmp_path, monkeypatch):
    async with _editor(tmp_path, monkeypatch, LossyResume) as e:
        for ask, reply in (('first ask', 'first reply'), ('second ask', 'second reply')):
            e.db.append_message(e.sid, 'user', ask)
            e.db.append_message(e.sid, 'assistant', reply)
        with pytest.raises(GatewayClientError, match='disconnected'):
            await asyncio.wait_for(e.agent.prompt([acp.text_block('/retry')], e.sid), 5)
        assert _users(e.db, e.sid) == ['first ask']  # the rewind committed before the refresh failed
        response = await asyncio.wait_for(e.agent.prompt([acp.text_block('/retry')], e.sid), 5)
        assert response.stop_reason == 'end_turn'
        # The repeat replays the original rewind: the earlier turn stays, the retried text is the same.
        assert e.executed == ['second ask'], e.executed
        assert _users(e.db, e.sid)[0] == 'first ask', _users(e.db, e.sid)

@pytest.mark.asyncio
@pytest.mark.parametrize('persist_answer', [False, True])
@pytest.mark.parametrize('settled_before_retry', [False, True])
async def test_retry_submit_ack_loss_preserves_final_payload_and_history(
        tmp_path, monkeypatch, persist_answer, settled_before_retry):
    from hermes_state_runtime import list_session_admissions
    from tests.waiting import async_wait_until

    async with _editor(tmp_path, monkeypatch, persist_answer=persist_answer) as e:
        for ask, reply in (('first ask', 'first reply'), ('second ask', 'second reply')):
            e.db.append_message(e.sid, 'user', ask)
            e.db.append_message(e.sid, 'assistant', reply)
        submits = []
        rpc = e.wire.rpc

        async def observe(method, **params):
            if method == 'prompt.submit':
                submits.append(dict(params))
            return await rpc(method, **params)

        e.wire.rpc = observe
        e.wire.lose_ack = True
        schedule = e.authority._schedule
        if not settled_before_retry:
            e.authority._schedule = lambda ref: None
        try:
            with pytest.raises(GatewayClientError, match='outcome is unknown'):
                await asyncio.wait_for(e.agent.prompt([acp.text_block('/retry')], e.sid), 5)
        finally:
            e.authority._schedule = schedule
        if settled_before_retry:
            await async_wait_until(lambda: 'ACK_second ask' in e.updates, timeout=5,
                                   what='the admitted retry reply before retransmission')
        response = await asyncio.wait_for(e.agent.prompt([acp.text_block('/retry')], e.sid), 5)
        assert response.stop_reason == 'end_turn'
        assert len(submits) == 2 and submits[0] == submits[1]
        assert submits[0]['text'] == 'second ask'
        assert len(list_session_admissions(e.db, session_id=e.sid, pending_only=False)) == 1
        assert e.executed == ['second ask']
        assert e.db.get_session(e.sid)['rewind_count'] == 1
        assert _users(e.db, e.sid) == (['first ask', 'second ask'] if persist_answer else ['first ask'])


@pytest.mark.asyncio
async def test_prepared_literal_slash_retransmission_bypasses_local_command(tmp_path, monkeypatch):
    from hermes_state_runtime import list_session_admissions
    from tests.waiting import async_wait_until

    async with _editor(tmp_path, monkeypatch) as e:
        submits = []
        rpc = e.wire.rpc

        async def observe(method, **params):
            if method == 'prompt.submit':
                submits.append(dict(params))
            return await rpc(method, **params)

        e.wire.rpc = observe
        e.wire.lose_ack = True
        with pytest.raises(GatewayClientError, match='outcome is unknown'):
            await asyncio.wait_for(e.agent.prompt([acp.text_block('/queue /help')], e.sid), 5)
        await async_wait_until(lambda: 'ACK_/help' in e.updates, timeout=5, what='the literal slash reply')
        response = await asyncio.wait_for(e.agent.prompt([acp.text_block('/queue /help')], e.sid), 5)
        assert response.stop_reason == 'end_turn'
        assert len(submits) == 2 and submits[0] == submits[1]
        assert submits[0]['text'] == '/help'
        assert e.executed == ['/help']
        assert len(list_session_admissions(e.db, session_id=e.sid, pending_only=False)) == 1
        assert e.updates == ['ACK_/help']


@pytest.mark.asyncio
@pytest.mark.parametrize('editor_alias', [False, True])
@pytest.mark.parametrize('boundary', ['history', 'rewind', 'refresh'])
async def test_stop_during_owner_backed_slash_preparation_prevents_follow_on_submit(
        tmp_path, monkeypatch, editor_alias, boundary):
    from hermes_state_runtime import list_session_admissions

    async with _editor(tmp_path, monkeypatch) as e:
        for ask, reply in (('first ask', 'first reply'), ('second ask', 'second reply')):
            e.db.append_message(e.sid, 'user', ask)
            e.db.append_message(e.sid, 'assistant', reply)
        held = 'editor-alias' if editor_alias else e.sid
        if editor_alias:
            e.agent._aliases[held] = e.sid
        reached, release = asyncio.Event(), asyncio.Event()
        rpc = e.wire.rpc

        async def pause_after_owner_rpc(method, **params):
            result = await rpc(method, **params)
            rewound = e.db.get_session(e.sid)['rewind_count'] > 0
            match = ((boundary == 'history' and method == 'session.resume' and not rewound)
                     or (boundary == 'rewind' and method == 'session.mutate'
                         and params.get('operation') == 'rewind')
                     or (boundary == 'refresh' and method == 'session.resume' and rewound))
            if match and not reached.is_set():
                reached.set()
                await release.wait()
            return result

        e.wire.rpc = pause_after_owner_rpc
        task = asyncio.create_task(e.agent.prompt([acp.text_block('/retry')], held))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            await e.agent.cancel(held)
            release.set()
            response = await asyncio.wait_for(task, 5)
            assert response.stop_reason == 'cancelled'
            assert not list_session_admissions(e.db, session_id=e.sid, pending_only=False)
            assert not e.executed
            assert e.db.get_session(e.sid)['rewind_count'] == 1
            assert _users(e.db, e.sid) == ['first ask']
            assert e.sid not in e.agent._submitting and e.sid not in e.agent._pending_cancels
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


@pytest.mark.asyncio
async def test_stop_during_ambiguous_submit_replay_cancels_exact_input_not_successor(tmp_path, monkeypatch):
    from hermes_state_runtime import list_session_admissions

    async with _editor(tmp_path, monkeypatch) as e:
        schedule = e.authority._schedule
        e.authority._schedule = lambda ref: None
        e.wire.lose_ack = True
        try:
            with pytest.raises(GatewayClientError, match='outcome is unknown'):
                await asyncio.wait_for(e.agent.prompt([acp.text_block('ambiguous work')], e.sid), 5)
            original = list_session_admissions(e.db, session_id=e.sid, pending_only=False)[0]
            reached, release = asyncio.Event(), asyncio.Event()
            client = e.agent._client

            async def reconnect():
                connection = await client()
                reached.set()
                await release.wait()
                return connection

            monkeypatch.setattr(e.agent, '_client', reconnect)
            task = asyncio.create_task(e.agent.prompt([acp.text_block('ambiguous work')], e.sid))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                await e.agent.cancel(e.sid)
                release.set()
                assert (await asyncio.wait_for(task, 5)).stop_reason == 'cancelled'
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            receipt = await e.wire.rpc('prompt.receipt', session_id=e.sid,
                                       admission_id=original['admission_id'])
            assert receipt['outcome'] == 'cancelled'
            assert not e.executed
        finally:
            e.authority._schedule = schedule
        monkeypatch.setattr(e.agent, '_client', client)
        assert (await asyncio.wait_for(e.agent.prompt([acp.text_block('fresh work')], e.sid), 5)).stop_reason == 'end_turn'
        assert e.executed == ['fresh work']
        assert len(list_session_admissions(e.db, session_id=e.sid, pending_only=False)) == 2
        assert e.sid not in e.agent._pending_cancels and e.sid not in e.agent._submitting
