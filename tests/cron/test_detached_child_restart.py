"""A surviving detached cron must not reconnect a retired child as its root execution."""
import threading
from functools import partial
from types import SimpleNamespace

import pytest

from agent.runtime_session_store import WorkerPersistenceError
from hermes_state import SessionDB
import hermes_state_runtime as rt
from tests.fakes.runtime_owner import begin_runtime_epoch


@pytest.mark.parametrize('fault', ['retired', 'lost_reply', 'parent_closed'])
def test_detached_child_restart_returns_failed_delegation(tmp_path, monkeypatch, fault):
    from agent import runtime_session_store as worker
    from agent.runtime_session_child import reserve_child_store
    from cron import detached_authority, scheduler
    from gateway import session_cron
    from gateway.session_worker_child import reserve_child_execution
    from tests.tools.test_delegate_output_schema import _StubChild, _StubParent
    from tools import delegate_tool

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB(tmp_path / 'owner.db')
    db.create_session('parent', 'cron')
    state = SimpleNamespace(epoch=begin_runtime_epoch(db, instance_id='first'),
                            stores=[], adoptions=[], injected=False, verdicts=[], result=None, error=None)
    done, abort = threading.Event(), threading.Event()
    monkeypatch.setattr(worker, '_worker_process', False)
    monkeypatch.setattr(detached_authority, 'owns_detached_execution', lambda *args: True)
    monkeypatch.setattr(delegate_tool, '_get_worktree_isolation', lambda: False)
    real_bind = session_cron.bind_execution

    def bind(owner, *args):
        state.stores.append(owner.db)
        return real_bind(owner, *args)

    monkeypatch.setattr(session_cron, 'bind_execution', bind)

    class OwnerRPC:
        """In-process transport; the detached wrapper, child facade and owner ledger are real."""
        def __init__(self, home):
            pass

        def close(self):
            pass

        def __call__(self, method, **params):
            if abort.is_set():
                raise WorkerPersistenceError('test_deadline')
            assignment = {k: params[k] for k in ('execution_id', 'session_id', 'generation')}
            try:
                if method == 'worker.register':
                    return rt.register_worker_execution(db, epoch=state.epoch, **assignment,
                        kind='cron', adoption_secret=params['secret'])
                if method == 'worker.adopt':
                    state.adoptions.append(assignment['execution_id'])
                    return rt.adopt_worker_execution(db, epoch=state.epoch, **assignment,
                                                     adoption_secret=params['secret'])
                if params['operation'] == 'child.reserve':
                    handler = partial(reserve_child_execution, epoch=state.epoch,
                        execution_id=params['execution_id'], principal='local', limits=(2, 1), refused=None,
                        identity={k: params[k] for k in ('profile_id', 'pid', 'birth')})
                    return rt.apply_worker_receipt(db, **assignment,
                        **{k: params[k] for k in ('epoch', 'sequence', 'operation', 'payload')},
                        handlers={'child.reserve': handler})
                if fault != 'retired' and not state.injected and '/child:' in params['execution_id']:
                    state.injected = True
                    if fault == 'parent_closed':
                        state.stores[0].close()
                    raise TimeoutError('worker_receipt_timeout')
                return rt.mutate_worker_execution(db, **assignment,
                    **{k: params[k] for k in ('epoch', 'sequence', 'operation', 'payload')})
            except rt.RuntimeStoreError as exc:
                if len(state.verdicts) < 10:
                    state.verdicts.append(str(exc))
                raise WorkerPersistenceError(str(exc)) from exc

    monkeypatch.setattr(worker, 'WorkerRPC', OwnerRPC)

    def compute(*args, **kwargs):
        parent_store = state.stores[0]
        child_store = reserve_child_store(parent_store)
        state.stores.append(child_store)
        child = _StubChild([])
        child.session_id = child_store.reserved_session_id

        def conversation(**kwargs):
            # Active child crossed a gateway restart, while the detached root survived.
            state.epoch = begin_runtime_epoch(db, instance_id='successor')
            rt.recover_session_inputs(db, epoch=state.epoch)
            child_store.append_messages_batch(child.session_id, [{'role': 'assistant', 'content': 'late'}])
            return {'completed': True, 'final_response': 'must not succeed'}

        child.run_conversation = conversation
        result = delegate_tool._run_single_child(0, 'persist child completion', child, _StubParent())
        return False, 'output', result, None

    monkeypatch.setattr(scheduler, 'run_job', compute)

    def run():
        try:
            state.result = detached_authority.run_detached(
                {'id': 'inert'}, {'session_id': 'parent'}, {'profile_id': str(tmp_path), 'generation': 0},
                {}, lambda: None, execution_id='inert', extra_prompt=None, cancel_event=None)
        except Exception as exc:
            state.error = exc
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert done.wait(5), 'retired child looped past the bounded delegation deadline'
        assert state.error is None
        result = state.result[2]
        assert result['status'] in ('error', 'failed'), result
        assert 'child execution retired' in result['error'], result
        assert 'stale_generation' in result['error'], result
        assert 'outbox_closed' not in str(result)
        child_store = state.stores[1]
        assert child_store.failure == result['error']
        assert db.get_messages(child_store.reserved_session_id) == []
        # No root adoption on the child's behalf, nor cross-execution journal mutation.
        root = state.stores[0]
        assert state.adoptions == [root.scope['execution_id']] + (
            [] if fault == 'retired' else [child_store.scope['execution_id']])
        assert root.scope['epoch'] == 1
        assert state.verdicts == ['stale_generation']
    finally:
        abort.set()
        thread.join(5)
        assert not thread.is_alive(), 'test cleanup failed to release the reconnect loop'
        for store in state.stores:
            store._outbox_owner.close()
        db.close()
