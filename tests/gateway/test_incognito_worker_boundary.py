"""Privacy entry excludes live durable lineage work without cancelling its assignment."""

from copy import deepcopy
from functools import partial
import os
import threading
from types import SimpleNamespace

import pytest
import psutil

from agent.incognito import is_registered, register_session
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from gateway.session_worker_child import reserve_child_execution
from hermes_cli.process_identity import worker_birth
from hermes_state import SessionDB
import hermes_state_runtime as rt
from tests.fakes.runtime_owner import begin_runtime_epoch


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB(tmp_path / 'state.db')
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    # Pin this test-owned handle, avoiding the shared handle registry entirely.
    store._db = db
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='boundary', thread_id='17', user_id='human')
    entry = store.get_or_create_session(source)
    entry.model_override = {'model': 'public-model', 'provider': 'public-provider'}
    entry.model_override_scope = 'session'
    entry.model_role = 'public-role'
    store.update_session(entry.session_key)
    store.append_to_transcript(entry.session_id, {'role': 'user', 'content': 'PUBLIC_saved_prefix'})
    store.append_to_transcript(entry.session_id, {'role': 'assistant', 'content': 'PUBLIC_saved_reply'})
    epoch = begin_runtime_epoch(db, instance_id='boundary-owner')
    case = SimpleNamespace(db=db, store=store, entry=entry, epoch=epoch, ids={entry.session_id})
    try:
        yield case
    finally:
        for session_id in case.ids | {entry.session_id, entry.session_key}:
            register_session(session_id, False)
        db.close()


def _register(case, target=None):
    target = target or case.entry.session_id
    return rt.register_worker_execution(
        case.db, epoch=case.epoch, execution_id='durable-worker', session_id=target,
        generation=case.db.get_session(target)['runtime_generation'], kind='compute', adoption_secret='fixture-secret')


def _worker_scope(case):
    row = case.db._read_one('SELECT * FROM worker_executions WHERE execution_id=?', ('durable-worker',))
    return dict(epoch=case.epoch, execution_id=row['execution_id'], session_id=row['session_id'],
                generation=row['generation'])


def _public_snapshot(case):
    store, entry = case.store, case.entry
    # Include both durable routing surfaces and the optional privacy sidecar.
    files = {name: (store.sessions_dir / name).read_bytes() if (store.sessions_dir / name).exists() else None
             for name in ('sessions.json', 'incognito.json')}
    routing = [tuple(row) for row in case.db._read_all('SELECT * FROM gateway_routing ORDER BY session_key')]
    return deepcopy(entry.__dict__), files, routing, store.load_transcript(entry.session_id)


def _assert_public_unchanged(case, before):
    assert _public_snapshot(case) == before
    assert case.store.lookup_by_session_key(case.entry.session_key) is case.entry
    assert not case.entry.incognito
    assert not case.store.incognito_enabled(case.entry.session_key)
    assert not is_registered(case.entry.session_id)
    assert not is_registered(case.entry.session_key)


def _compress(case, arrangement):
    root = case.entry.session_id
    if arrangement == 'same':
        return root
    child = root + '-compressed'
    case.ids.add(child)
    assert case.db.try_acquire_compression_lock(root, 'fixture-compression')
    case.db.publish_compression_child(
        parent_session_id=root, child_session_id=child, source='telegram',
        messages=case.store.load_transcript(root), system_prompt='PUBLIC_saved_system_prefix',
        compression_lock_holder='fixture-compression')
    if arrangement == 'ancestor_worker':
        # The public route now names the physical continuation, its worker the ancestor.
        case.entry.session_id = child
        case.store.update_session(case.entry.session_key)
        return root
    # The logical route still names the root, its worker the physical continuation.
    return child


@pytest.mark.parametrize('status', ['registered', 'running', 'unknown'])
@pytest.mark.parametrize('arrangement', ['same', 'ancestor_worker', 'physical_worker'])
def test_live_durable_lineage_refuses_entry_without_any_partial_boundary(boundary, status, arrangement):
    case = boundary
    target = _compress(case, arrangement)
    assert _register(case, target)['status'] == 'registered'
    if status == 'running':
        assert rt.adopt_worker_execution(case.db, **_worker_scope(case),
                                        adoption_secret='fixture-secret')['status'] == 'running'
    elif status == 'unknown':
        case.epoch = begin_runtime_epoch(case.db, instance_id='replacement-owner')
        rt.recover_session_inputs(case.db, epoch=case.epoch)
    assignment = tuple(case.db._read_one('SELECT * FROM worker_executions'))
    assert case.db._read_one('SELECT status FROM worker_executions')[0] == status
    before = _public_snapshot(case)
    with pytest.raises((ValueError, rt.RuntimeStoreError)):
        case.store.set_incognito(case.entry.session_key, True,
                                  active_override={'model': 'private-model', 'provider': 'private-provider'})
    _assert_public_unchanged(case, before)
    assert tuple(case.db._read_one('SELECT * FROM worker_executions')) == assignment


def test_terminal_durable_work_allows_entry_and_restores_public_prefix_and_model(boundary):
    case = boundary
    _register(case)
    rt.mutate_worker_execution(case.db, **_worker_scope(case), sequence=1, operation='execution.finish', payload={})
    assignment = tuple(case.db._read_one('SELECT * FROM worker_executions'))
    before = _public_snapshot(case)
    case.store.set_incognito(case.entry.session_key, True,
                             active_override={'model': 'private-model', 'provider': 'private-provider'})
    assert case.entry.incognito and case.store.incognito_enabled(case.entry.session_key)
    assert case.entry.model_override['model'] == 'private-model'
    assert case.store.load_transcript(case.entry.session_id) == before[3]
    assert tuple(case.db._read_one('SELECT * FROM worker_executions')) == assignment
    case.store.set_incognito(case.entry.session_key, False)
    assert case.entry.__dict__ == before[0]
    assert case.store.load_transcript(case.entry.session_id) == before[3]
    assert not is_registered(case.entry.session_id)
    assert not is_registered(case.entry.session_key)


@pytest.mark.parametrize('operation', ['register', 'adopt', 'child.reserve'])
def test_private_target_explicitly_refuses_new_durable_authority_before_sql(boundary, operation):
    case = boundary
    if operation != 'register':
        _register(case)
    # An established private target must fence even an already-issued stale assignment.
    # The transition tests separately prove this state cannot be newly reached by a live worker.
    if operation == 'register':
        case.store.set_incognito(case.entry.session_key, True)
    else:
        register_session(case.entry.session_id)
    before = tuple(case.db._conn.iterdump())
    if operation == 'child.reserve':
        scope = _worker_scope(case)
        handler = partial(reserve_child_execution, epoch=case.epoch, execution_id='durable-worker',
                          identity={'profile_id': str(case.db.db_path.parent), 'pid': os.getpid(),
                                    'birth': worker_birth(psutil.Process())},
                          principal='human', limits=(2, 1), refused=None)
        invoke = lambda: rt.apply_worker_receipt(case.db, **scope, sequence=1, operation='child.reserve',
            payload={'secret': 'child-fixture-secret'}, handlers={'child.reserve': handler})
    sql = []
    # Resolve test fixture reads before tracing the actual durable operation.
    if operation == 'register':
        scope = dict(epoch=case.epoch, execution_id='durable-worker', session_id=case.entry.session_id,
                     generation=case.db.get_session(case.entry.session_id)['runtime_generation'])
        invoke = lambda: rt.register_worker_execution(case.db, **scope, kind='compute', adoption_secret='fixture-secret')
    elif operation == 'adopt':
        scope = _worker_scope(case)
        invoke = lambda: rt.adopt_worker_execution(case.db, **scope, adoption_secret='fixture-secret')
    case.db._conn.set_trace_callback(sql.append)
    try:
        with pytest.raises(rt.RuntimeStoreError, match='^privacy_denied$'):
            invoke()
    finally:
        case.db._conn.set_trace_callback(None)
    assert sql == []
    assert tuple(case.db._conn.iterdump()) == before


class _ObservedLock:
    """Observe contention on the actual writer lock without changing its semantics."""

    def __init__(self, inner, thread_name, attempted):
        self.inner, self.thread_name, self.attempted = inner, thread_name, attempted

    def acquire(self, *args, **kwargs):
        if threading.current_thread().name == self.thread_name:
            self.attempted.set()
        return self.inner.acquire(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_args):
        self.inner.release()


def _run_thread(name, call, outcomes, finished=None):
    def run():
        try:
            outcomes[name] = call()
        except BaseException as exc:
            outcomes[name] = exc
        finally:
            if finished is not None:
                finished.set()
    thread = threading.Thread(name=name, target=run, daemon=True)
    thread.start()
    return thread


def test_toggle_winning_publication_refuses_contending_registration(boundary, monkeypatch):
    case = boundary
    import gateway.session_incognito as privacy
    publishing, release, attempted = threading.Event(), threading.Event(), threading.Event()
    write = privacy.atomic_json_write

    def pause_publication(path, data, **kwargs):
        if path == case.store._incognito_path():
            publishing.set()
            assert release.wait(10), 'boundary publication was never released'
        return write(path, data, **kwargs)

    monkeypatch.setattr(privacy, 'atomic_json_write', pause_publication)
    monkeypatch.setattr(case.db, '_lock', _ObservedLock(case.db._lock, 'register-contender', attempted))
    outcomes, threads = {}, []
    try:
        threads.append(_run_thread('toggle-winner', lambda: case.store.set_incognito(case.entry.session_key, True), outcomes))
        assert publishing.wait(10), 'toggle did not reach real privacy publication'
        threads.append(_run_thread('register-contender', lambda: _register(case), outcomes, finished=attempted))
        assert attempted.wait(10), 'registration neither reached the writer lock nor returned a refusal'
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
    assert all(not thread.is_alive() for thread in threads), 'privacy/DB lock order deadlocked'
    assert not isinstance(outcomes['toggle-winner'], BaseException)
    refusal = outcomes['register-contender']
    assert isinstance(refusal, rt.RuntimeStoreError) and str(refusal) == 'privacy_denied'
    assert case.entry.incognito and case.store.incognito_enabled(case.entry.session_key)
    assert case.db._read_one('SELECT COUNT(*) FROM worker_executions')[0] == 0


def test_registration_winning_transaction_refuses_contending_toggle(boundary, monkeypatch):
    case = boundary
    writing, release, attempted = threading.Event(), threading.Event(), threading.Event()
    trace_errors = []

    def pause_insert(statement):
        if statement.lstrip().upper().startswith('INSERT INTO WORKER_EXECUTIONS'):
            writing.set()
            if not release.wait(10):
                # sqlite trace callback exceptions are swallowed, so retain failures explicitly.
                trace_errors.append('worker insert was never released')

    lookup = case.store.lookup_by_session_key

    def observe_lookup(key):
        entry = lookup(key)
        if threading.current_thread().name == 'toggle-contender':
            attempted.set()
        return entry

    monkeypatch.setattr(case.store, 'lookup_by_session_key', observe_lookup)
    before = _public_snapshot(case)
    outcomes, threads = {}, []
    case.db._conn.set_trace_callback(pause_insert)
    try:
        threads.append(_run_thread('register-winner', lambda: _register(case), outcomes))
        assert writing.wait(10), 'registration did not reach its real transaction insert'
        threads.append(_run_thread('toggle-contender', lambda: case.store.set_incognito(case.entry.session_key, True), outcomes))
        assert attempted.wait(10), 'toggle did not reach its real public routing lookup'
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
        case.db._conn.set_trace_callback(None)
    assert all(not thread.is_alive() for thread in threads), 'privacy/DB lock order deadlocked'
    assert not trace_errors
    assert outcomes['register-winner']['status'] == 'registered'
    assert isinstance(outcomes['toggle-contender'], (ValueError, rt.RuntimeStoreError))
    _assert_public_unchanged(case, before)
    assert case.db._read_one('SELECT status,last_sequence FROM worker_executions')[:] == ('registered', 0)
