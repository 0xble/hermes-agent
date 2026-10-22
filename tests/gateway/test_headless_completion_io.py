"""Producer capture shares the admission FIFO and keeps live publication on-loop."""
import asyncio
import contextvars
import threading
from types import SimpleNamespace

import pytest
import pytest_asyncio

from gateway import session_automation as automation
from gateway import session_local_recovery as recovery
from gateway.session_runtime_workers import mutation_tasks, tracked_write
from hermes_state_runtime import RuntimeStoreError, list_session_admissions


@pytest.mark.asyncio
async def test_slow_capture_reserves_fifo_and_survives_caller_cancellation():
    authority = SimpleNamespace(runner=None)
    entered, release = asyncio.Event(), asyncio.Event()
    commits, callbacks = [], []
    scope = contextvars.ContextVar('producer_scope', default='wrong')
    scope.set('owning-profile')
    loop_thread = threading.get_ident()

    async def prepare():
        entered.set()
        await release.wait()
        def write():
            assert threading.get_ident() != loop_thread
            assert scope.get() == 'owning-profile'
            commits.append('producer')
            return 'producer'
        return write

    def published(result):
        assert threading.get_ident() == loop_thread
        callbacks.append(('publish', result))
        return result

    producer = asyncio.create_task(tracked_write(authority, None, published, ordered=True,
        prepare=prepare, after=lambda result: callbacks.append(('schedule', result))))
    await entered.wait()
    human = asyncio.create_task(tracked_write(authority, lambda: commits.append('human'), ordered=True))
    await asyncio.sleep(0)
    assert commits == []
    producer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await producer
    assert mutation_tasks(authority), 'retirement must still join the capture'
    release.set()
    await human
    await asyncio.gather(*mutation_tasks(authority))
    await asyncio.sleep(0)
    assert commits == ['producer', 'human']
    assert callbacks == [('publish', 'producer'), ('schedule', 'producer')]
    assert mutation_tasks(authority) == []


@pytest_asyncio.fixture
async def local_owner(tmp_path, monkeypatch):
    from tests.gateway.test_local_create_env_refs import _owner
    from gateway.session_local import create_local_session
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('platform_toolsets: {cli: []}\n'
        'model: {provider: custom, default: m, base_url: "http://127.0.0.1:9/v1"}\n')
    owner, connection = await _owner(home, monkeypatch)
    ref = create_local_session(owner, connection.actor, dict(request_id='producer', source='cli',
                                                           cwd=str(home), toolsets=[]))
    owner.runner._adapter_for_source = lambda source: owner.runner.adapters.get(source.platform)
    owner._schedule = lambda ref: None
    try:
        yield owner, ref
    finally:
        import hermes_loop_guard
        hermes_loop_guard.set_loop_thread(None)
        await asyncio.gather(*mutation_tasks(owner))
        await connection.close()
        owner.db.close()


def _strict(monkeypatch):
    import hermes_loop_guard
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'strict-producer-capture')
    hermes_loop_guard.set_loop_thread(threading.get_ident())


def _event(owner, ref, identity='completion-1'):
    from gateway.platforms.event import MessageEvent
    live = owner.sessions[ref.session_id]
    return MessageEvent(text='real completion output', source=live.source, internal=True,
        message_id=identity, metadata={'gateway_session_key': live.route,
                                     'gateway_session_id': ref.session_id})


@pytest.mark.asyncio
async def test_capture_io_off_loop_live_snapshot_and_publication_on_loop(local_owner, monkeypatch):
    owner, ref = local_owner
    event = _event(owner, ref)
    loop_thread, checks = threading.get_ident(), []
    for module, name in [(automation, '_owner'), (recovery, '_read_local_session'),
                         (recovery, '_restore_local_entry')]:
        original = getattr(module, name)
        def checked(*args, _original=original, _name=name, **kwargs):
            assert threading.get_ident() != loop_thread, _name
            checks.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(module, name, checked)
    for module, name in [(automation, 'snapshot_automation'), (recovery, '_publish_local_session')]:
        original = getattr(module, name)
        def published(*args, _original=original, _name=name, **kwargs):
            assert threading.get_ident() == loop_thread, _name
            checks.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(module, name, published)
    original_publish = owner._publish_pending
    def publish_pending(ref):
        assert threading.get_ident() == loop_thread
        checks.append('pending')
        return original_publish(ref)
    owner._publish_pending = publish_pending
    _strict(monkeypatch)
    first = await owner.admit_automation(owner.runner.adapters[event.source.platform], event, 'completion-1')
    replay = await owner.admit_automation(owner.runner.adapters[event.source.platform], event, 'completion-1')
    assert first.admission_id == replay.admission_id
    rows = await asyncio.to_thread(list_session_admissions, owner.db, session_id=ref.session_id)
    assert len(rows) == 1 and rows[0]['payload']['text'] == 'real completion output'
    assert set(checks) == {'_owner', '_read_local_session', '_restore_local_entry',
                           'snapshot_automation', '_publish_local_session', 'pending'}


@pytest.mark.asyncio
async def test_cold_local_restore_preserves_policy_toggle_and_publishes_on_loop(local_owner, monkeypatch):
    owner, ref = local_owner
    from gateway.config import Platform
    old = owner.sessions.pop(ref.session_id)
    owner.runner.adapters.pop(Platform.LOCAL)
    owner.runner.session_store._entries[old.route].yolo = False
    original, publications = recovery._publish_local_session, []
    loop_thread = threading.get_ident()
    def publish(*args):
        assert threading.get_ident() == loop_thread
        publications.append(True)
        return original(*args)
    monkeypatch.setattr(recovery, '_publish_local_session', publish)
    _strict(monkeypatch)
    assert await recovery.restore_local_session_async(owner, ref.session_id) == ref
    live = owner.sessions[ref.session_id]
    adapter = owner.runner.adapters[Platform.LOCAL]
    assert publications == [True] and adapter.authorize_source(live.source)
    entry = await asyncio.to_thread(owner.runner.session_store.lookup_by_session_key, old.route)
    assert entry.yolo is False and entry.session_id == ref.session_id


@pytest.mark.asyncio
async def test_live_local_capture_still_refuses_invalid_durable_policy(local_owner, monkeypatch):
    owner, ref = local_owner
    event = _event(owner, ref)
    from hermes_state_local import POLICY_PREFIX
    import sqlite3
    with sqlite3.connect(owner.db.db_path) as conn:
        conn.execute('DELETE FROM state_meta WHERE key=?', (POLICY_PREFIX + ref.session_id,))
    _strict(monkeypatch)
    with pytest.raises(RuntimeStoreError):
        await owner.admit_automation(owner.runner.adapters[event.source.platform], event, 'completion-1')
    rows = await asyncio.to_thread(list_session_admissions, owner.db, session_id=ref.session_id)
    assert rows == []


@pytest.mark.asyncio
async def test_producer_capture_cancellation_cannot_reorder_human_admission(local_owner, monkeypatch):
    owner, ref = local_owner
    event = _event(owner, ref)
    entered, release = threading.Event(), threading.Event()
    commits, scheduled = [], []
    original_owner, original_admit = automation._owner, automation.admit_session_input
    def capture(*args):
        entered.set()
        assert release.wait(10)
        return original_owner(*args)
    def commit(*args, **kwargs):
        row = original_admit(*args, **kwargs)
        commits.append('producer')
        return row
    monkeypatch.setattr(automation, '_owner', capture)
    monkeypatch.setattr(automation, 'admit_session_input', commit)
    owner._schedule = lambda ref: scheduled.append(ref)
    _strict(monkeypatch)
    producer = asyncio.create_task(owner.admit_automation(
        owner.runner.adapters[event.source.platform], event, 'completion-1'))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        human = asyncio.create_task(tracked_write(owner, lambda: commits.append('human'), ordered=True))
        await asyncio.sleep(0)
        assert commits == []
        producer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await producer
        assert mutation_tasks(owner)
    finally:
        release.set()
    await human
    await asyncio.gather(*mutation_tasks(owner))
    await asyncio.sleep(0)
    assert commits == ['producer', 'human'] and scheduled == [ref]
    rows = await asyncio.to_thread(list_session_admissions, owner.db, session_id=ref.session_id)
    assert len(rows) == 1 and event._gateway_accepted
