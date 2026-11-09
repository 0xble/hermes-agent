"""Compression-held native turns retain their durable claim, not a RAM retry slot."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from hermes_state_runtime import list_session_admissions
from tests.fakes.runtime_owner import initialize_session_authority
from tests.gateway.test_shared_busy_receive_loop import Adapter
from tests.waiting import async_wait_until


async def _owner(monkeypatch, tmp_path, durable_outbox=False):
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'fixture-user')
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions', durable_outbox_enabled=durable_outbox))
    adapter = Adapter()
    adapter.gateway_runner = runner
    runner.adapters = {Platform.TELEGRAM: adapter}
    owner = await initialize_session_authority(runner, profile_id='default', instance_id='compression-owner')
    runner._wire_adapter_handlers(adapter)
    monkeypatch.setattr(runner, '_read_user_config', lambda: {'approvals': {'destructive_slash_confirm': False}})
    source = adapter.build_source(chat_id='compression-chat', chat_type='dm', user_id='fixture-user')
    ref = owner.register(source)
    key = runner.session_store._generate_session_key(source)
    return runner, adapter, owner, source, ref, key


def _rows(owner, ref):
    return list_session_admissions(owner.db, session_id=ref.session_id, pending_only=False)


def _complete_model_turn(runner, kwargs):
    """Stand in for the model/core only; use real transcript writes and result capture."""
    from gateway.session_results import execution_result

    text = kwargs['persist_user_message']
    suffix = [{'role': 'user', 'content': text}, {'role': 'assistant', 'content': f'REPLY_{text}'}]
    entry = runner.session_store.get_or_create_session(kwargs['source'])
    for message in suffix:
        runner.session_store.append_to_transcript(entry.session_id, message)
    result = {'final_response': f'REPLY_{text}', 'messages': [*kwargs['history'], *suffix],
              'api_calls': 1, 'history_offset': len(kwargs['history'])}
    execution_result.get()['result'] = result
    return result


async def _cleanup(runner, adapter, owner):
    runner._draining = True
    tasks = [live.task for live in owner.sessions.values() if live.task and not live.task.done()]
    tasks += list(adapter._background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    cleanup = getattr(runner, '_deferred_agent_cleanup_tasks', None) or set()
    if cleanup:
        await asyncio.wait_for(asyncio.gather(*list(cleanup), return_exceptions=True), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize('during_hygiene', [False, True], ids=['held-before-entry', 'hygiene-starts-lock'])
@pytest.mark.parametrize('durable_outbox', [False, True], ids=['without-outbox', 'durable-outbox'])
async def test_native_compression_deferral_keeps_claim_until_commit(monkeypatch, tmp_path, during_hygiene, durable_outbox):
    runner, adapter, owner, source, ref, key = await _owner(monkeypatch, tmp_path, durable_outbox)
    db, sid = owner.db, ref.session_id
    started, release, committed = threading.Event(), threading.Event(), threading.Event()
    executions, hooks = [], []

    async def post_turn(**kwargs):
        hooks.append(kwargs['event'].text)

    monkeypatch.setattr(runner, '_run_post_turn_hooks', post_turn)
    summary = [{'role': 'assistant', 'content': 'committed summary'}]
    db.append_message(sid, 'user', 'old history')
    db.append_message(sid, 'assistant', 'old answer')
    db.append_message(sid, 'user', 'more history')
    db.append_message(sid, 'assistant', 'more answer')
    monkeypatch.setattr('gateway.run._resolve_runtime_agent_kwargs', lambda: {'api_key': 'fake'})

    class Compressor:
        def __init__(self, **kwargs):
            self.session_id = sid
            self.context_compressor = SimpleNamespace(
                bind_session_state=MagicMock(), _last_compress_aborted=False,
                _last_aux_model_failure_model=None,
            )
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()

        def _compress_context(self, messages, *_args, commit_fence=None, **kwargs):
            assert db.try_acquire_compression_lock(sid, 'fixture-compressor')
            commit_fence.mark_commit_watermark_fenced()
            started.set()
            try:
                assert release.wait(30), 'test did not release compressor'
                assert commit_fence.begin_commit()
                try:
                    _commit()
                    self._last_compaction_in_place = True
                    return summary, None
                finally:
                    commit_fence.finish_commit()
            finally:
                db.release_compression_lock(sid, 'fixture-compressor')

    def _commit():
        # A real committed transcript replacement, not a mocked history reader.
        db.archive_and_compact(sid, summary)
        committed.set()

    if during_hygiene:
        monkeypatch.setattr('run_agent.AIAgent', Compressor)
        settings = runner._hmwa_hygiene_settings

        async def hygiene_settings(*args):
            hs = await settings(*args)
            hs.hard_msg_limit = 1000 if committed.is_set() else 2
            hs.compression_enabled = not committed.is_set()
            hs.max_turn_hold_seconds = 0.01
            hs.timeout_seconds = 60
            return hs

        monkeypatch.setattr(runner, '_hmwa_hygiene_settings', hygiene_settings)
        waiter = runner._hmwa_hygiene_wait_for_summary

        async def wait_for_started(attempt, hs, entry):
            assert await asyncio.to_thread(started.wait, 5)
            return await waiter(attempt, hs, entry)

        monkeypatch.setattr(runner, '_hmwa_hygiene_wait_for_summary', wait_for_started)
    else:
        assert db.try_acquire_compression_lock(sid, 'fixture-compressor')
        started.set()

    async def model(**kwargs):
        assert committed.is_set(), 'model ran before compression committed'
        text = kwargs['persist_user_message']
        assert kwargs['history'][0]['role'] == 'assistant'
        assert kwargs['history'][0]['content'] == 'committed summary'
        executions.append(text)
        return _complete_model_turn(runner, kwargs)

    deferred = asyncio.Event()
    defer = runner._defer_compression_locked_event

    async def observe_deferral(*args, **kwargs):
        result = await defer(*args, **kwargs)
        if result:
            deferred.set()
        return result

    monkeypatch.setattr(runner, '_defer_compression_locked_event', observe_deferral)
    monkeypatch.setattr(runner, '_run_agent', model)
    try:
        await adapter.handle_message(MessageEvent(text='A', source=source, message_id='a'))
        await async_wait_until(lambda: started.is_set() and len(_rows(owner, ref)) == 1, timeout=10)
        # Observe the real shared deferral after the hygiene hold expires.
        await asyncio.wait_for(deferred.wait(), 10)
        await async_wait_until(lambda: not runner._is_session_running(key), timeout=10)
        await adapter.handle_message(MessageEvent(text='B', source=source, message_id='b'))
        await adapter.handle_message(MessageEvent(text='C', source=source, message_id='c'))
        await async_wait_until(lambda: len(_rows(owner, ref)) == 3, timeout=10)
        before = _rows(owner, ref)
        assert [r['status'] for r in before] == ['started', 'queued', 'queued']
        assert executions == [] and hooks == [] and adapter.sent == []
        assert not adapter._pending_messages and not runner._overflow_queue(key)
        claim = before[0]['admission_id'], before[0]['generation']
        release.set()
        if not during_hygiene:
            _commit()
            db.release_compression_lock(sid, 'fixture-compressor')
        await async_wait_until(lambda: len(adapter.sent) == 3, timeout=15)
        assert executions == ['A', 'B', 'C']
        assert hooks == ['A', 'B', 'C']
        assert adapter.sent == ['REPLY_A', 'REPLY_B', 'REPLY_C']
        rows = _rows(owner, ref)
        assert [(r['status'], r['outcome']) for r in rows] == [('terminal', 'completed')] * 3
        assert (rows[0]['admission_id'], rows[0]['generation']) == claim
        if durable_outbox:
            from gateway.outbox import store_for
            outbox = store_for(runner._resolve_profile_home_for_source(source))
            for row in rows:
                transport_id = f"admission:{row['admission_id']}:{row['generation']}"
                assert outbox.lookup('default', 'telegram', transport_id, 'text')[1] == 'completed'
        assert [m['content'] for m in db.get_messages(sid)] == [
            'committed summary', 'A', 'REPLY_A', 'B', 'REPLY_B', 'C', 'REPLY_C',
        ]
    finally:
        release.set()
        if not during_hygiene:
            db.release_compression_lock(sid, 'fixture-compressor')
        await _cleanup(runner, adapter, owner)


@pytest.mark.asyncio
@pytest.mark.parametrize('control', ['stop', 'new', 'restart'])
async def test_compression_wait_honors_control_before_any_model_call(monkeypatch, tmp_path, control):
    from gateway.run_runtime import recover_profile_native_sessions

    runner, adapter, owner, source, ref, key = await _owner(monkeypatch, tmp_path)
    db, sid = owner.db, ref.session_id
    db.append_message(sid, 'assistant', 'earlier turn')
    assert db.try_acquire_compression_lock(sid, 'fixture-compressor')
    executions = []

    async def model(**kwargs):
        text = kwargs['persist_user_message']
        executions.append(text)
        return _complete_model_turn(runner, kwargs)

    monkeypatch.setattr(runner, '_run_agent', model)
    try:
        await adapter.handle_message(MessageEvent(text='A', source=source, message_id='a'))
        await async_wait_until(lambda: len(_rows(owner, ref)) == 1, timeout=10)
        await async_wait_until(lambda: _rows(owner, ref)[0]['status'] != 'queued', timeout=10)
        assert _rows(owner, ref)[0]['status'] == 'started'
        admission = _rows(owner, ref)[0]['admission_id']
        await adapter.handle_message(MessageEvent(text='B', source=source, message_id='b'))
        await async_wait_until(lambda: len(_rows(owner, ref)) == 2, timeout=10)
        if control in ('stop', 'new'):
            await adapter.handle_message(MessageEvent(text=f'/{control}', source=source, message_id='control'))
            await async_wait_until(lambda: all(r['status'] == 'terminal' for r in _rows(owner, ref)), timeout=10)
            assert _rows(owner, ref)[0]['outcome'] == 'interrupted'
            assert _rows(owner, ref)[1]['outcome'] == 'cancelled'
        else:
            runner._draining = True
            await async_wait_until(lambda: all(r['status'] == 'queued' for r in _rows(owner, ref)), timeout=10)
            await asyncio.wait_for(owner.sessions[sid].task, 10)
            # A real restart tears down the old adapter tasks before reconnecting.
            # Drain their pause replies before reusing this fixture adapter.
            await asyncio.wait_for(asyncio.gather(*list(adapter._session_tasks.values())), 10)
            # Busy-admission delivery observers are tracked separately from the
            # active transport task, and must unwind before adapter reuse too.
            await asyncio.wait_for(asyncio.gather(*list(adapter._background_tasks)), 10)
            assert executions == []
            # B's admission waiter may report the existing runtime-draining pause;
            # A itself is silently requeued, with no compression retry response.
            assert not any('compression' in reply.lower() for reply in adapter.sent)
            adapter.sent.clear()
            runner._draining = False
            owner = await initialize_session_authority(runner, profile_id='default', instance_id='successor')
            await recover_profile_native_sessions(runner, owner)
        db.release_compression_lock(sid, 'fixture-compressor')
        if control == 'restart':
            await async_wait_until(lambda: len(adapter.sent) == 2, timeout=15)
            assert executions == ['A', 'B'], (adapter.sent, _rows(owner, ref))
            assert adapter.sent == ['REPLY_A', 'REPLY_B']
            assert _rows(owner, ref)[0]['admission_id'] == admission
            assert all(r['status'] == 'terminal' and r['outcome'] == 'completed' for r in _rows(owner, ref))
        else:
            assert executions == []
            await adapter.handle_message(MessageEvent(text='C', source=source, message_id='c'))
            await async_wait_until(lambda: executions == ['C'], timeout=10)
    finally:
        runner._draining = False
        db.release_compression_lock(sid, 'fixture-compressor')
        await _cleanup(runner, adapter, owner)
