"""Detached persistence pauses across multiple bounded owner-connection windows."""
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def detached_worker(tmp_path, monkeypatch):
    from agent import runtime_session_store as worker
    from cron import detached_authority, scheduler
    from gateway import session_cron
    from hermes_cli import gateway_runtime
    from tools import async_delegation_worker

    clock = SimpleNamespace(now=0.0, until=0.0, waits=[], sent=[], stores=[], fault=None)

    def wait(seconds):
        clock.waits.append(seconds)
        clock.now += seconds

    timer = SimpleNamespace(monotonic=lambda: clock.now, sleep=wait)
    monkeypatch.setattr(worker, 'time', timer)
    monkeypatch.setattr(detached_authority, 'time', timer)
    monkeypatch.setattr(worker, '_worker_process', False)
    monkeypatch.setattr(async_delegation_worker, 'bind_worker_delegation_store', lambda store: None)
    monkeypatch.setattr(detached_authority, 'owns_detached_execution', lambda *args: True)
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda *args, **kwargs: SimpleNamespace(state='absent', endpoint=None))
    real_dial = worker.WorkerRPC._dial

    class Link:
        reader = SimpleNamespace(is_alive=lambda: True)
        ws = SimpleNamespace(close=lambda: None)

        def send(self, request_id, message):
            self.request = json.loads(message)
            clock.sent.append(self.request)
            if self.request['method'] == 'worker.persist' and clock.fault is not None:
                fault, clock.fault = clock.fault, None
                raise fault

        def receive(self, timeout):
            method = self.request['method']
            if method == 'worker.adopt':
                result = {'owner_epoch': 2 if clock.now else 1}
            elif method == 'worker.persist':
                result = {'count': 1, 'annotations': [{'_row_id': 7}]}
            else:
                result = {}
            return {'result': result}

    def dial(rpc, deadline):
        clock.rpc = rpc
        if clock.now < clock.until:
            pending = json.loads(clock.stores[0].path.read_text(encoding='utf-8-sig'))['pending']
            assert len(pending) == 1 and pending[0]['sequence'] == 1
            return real_dial(rpc, deadline)
        rpc.link = Link()
        return rpc.link

    monkeypatch.setattr(worker.WorkerRPC, '_dial', dial)
    real_bind = session_cron.bind_execution

    def bind(owner, *args):
        clock.stores.append(owner.db)
        return real_bind(owner, *args)

    monkeypatch.setattr(session_cron, 'bind_execution', bind)

    def run(*, outage=0.0, fault=None):
        def compute(*args, **kwargs):
            clock.until = outage
            clock.fault = fault
            # The old idle connection is gone before the next persistence call.
            store = clock.stores[0]
            rows = [{'role': 'assistant', 'content': 'computed once'}]
            clock.rpc.close()
            assert store.append_messages_batch('s', rows) == 1
            assert rows[0]['_row_id'] == 7
            return True, 'output', 'computed once', None

        monkeypatch.setattr(scheduler, 'run_job', compute)
        return detached_authority.run_detached(
            {'id': 'inert'}, {'session_id': 's'}, {'profile_id': str(tmp_path), 'generation': 0},
            {}, lambda: None, execution_id='inert', extra_prompt=None, cancel_event=None)

    try:
        yield clock, run
    finally:
        for store in clock.stores:
            store._outbox_owner.close()


def test_detached_persistence_survives_multiple_owner_retry_deadlines(detached_worker):
    from agent.runtime_session_store import OWNER_RESTART_TIMEOUT

    clock, run = detached_worker
    outage = OWNER_RESTART_TIMEOUT * 3 + 5
    assert run(outage=outage) == (True, 'output', 'computed once', None)
    assert clock.now >= outage
    store = clock.stores[0]
    assert store.failure is None and store.scope['epoch'] == 2
    assert json.loads(store.path.read_text(encoding='utf-8-sig'))['pending'] == []
    writes = [r['params'] for r in clock.sent if r['method'] == 'worker.persist']
    assert len(writes) == 1 and writes[0]['sequence'] == 1 and writes[0]['epoch'] == 2
    assert writes[0]['payload']['messages'] == [{'role': 'assistant', 'content': 'computed once'}]
    assert [r['method'] for r in clock.sent].count('worker.adopt') == 2


def test_detached_persistence_does_not_replay_chained_send_failure(detached_worker):
    from websockets.exceptions import ConnectionClosed

    clock, run = detached_worker
    fault = ConnectionClosed(None, None)
    fault.__cause__ = BrokenPipeError('bytes may have reached the owner')
    with pytest.raises(ConnectionClosed):
        run(fault=fault)
    assert [r['method'] for r in clock.sent] == ['worker.register', 'worker.adopt', 'worker.persist']
    store = clock.stores[0]
    assert len(json.loads(store.path.read_text(encoding='utf-8-sig'))['pending']) == 1
    assert clock.waits == []
