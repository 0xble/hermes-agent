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


@pytest.mark.parametrize('fault', ['lost_reply', 'double_entry_close', 'chained_write', 'receipt_timeout'])
def test_detached_receipt_replay_is_exactly_once(tmp_path, monkeypatch, fault):
    import threading

    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import serve

    from agent import runtime_session_store as worker
    from cron import detached_authority, scheduler
    from gateway import session_cron
    from hermes_cli import gateway_client, gateway_runtime, gateway_runtime_discovery as discovery
    import hermes_constants
    from hermes_state import SessionDB
    from hermes_state_runtime import (
        RuntimeStoreError, adopt_worker_execution, begin_runtime_epoch,
        mutate_worker_execution, register_worker_execution,
    )
    from tools import async_delegation_worker

    db = SessionDB(tmp_path / 'owner.db')
    db.create_session('s', 'cli')
    state = SimpleNamespace(epoch=begin_runtime_epoch(db, instance_id='first'),
                            requests=[], results=[], stores=[], sends=[], entry_closes=0,
                            committed=threading.Event(), injected=False, computes=0)
    monkeypatch.setattr(worker, '_worker_process', False)
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(async_delegation_worker, 'bind_worker_delegation_store', lambda store: None)
    monkeypatch.setattr(detached_authority, 'owns_detached_execution', lambda *args: True)
    real_bind = session_cron.bind_execution

    def bind(owner, *args):
        state.stores.append(owner.db)
        return real_bind(owner, *args)

    monkeypatch.setattr(session_cron, 'bind_execution', bind)

    def successor():
        state.epoch = begin_runtime_epoch(db, instance_id='successor')

    def handler(ws):
        for raw in ws:
            request = json.loads(raw)
            method, params = request['method'], request['params']
            state.requests.append((method, params))
            assignment = {key: params[key] for key in ('execution_id', 'session_id', 'generation')}
            try:
                if method == 'worker.register':
                    result = register_worker_execution(db, epoch=state.epoch, **assignment,
                        kind=params['kind'], adoption_secret=params['secret'])
                elif method == 'worker.adopt':
                    result = adopt_worker_execution(db, epoch=state.epoch, **assignment,
                                                    adoption_secret=params['secret'])
                else:
                    result = mutate_worker_execution(db, **assignment,
                        **{key: params[key] for key in ('epoch', 'sequence', 'operation', 'payload')})
                    state.results.append(result)
                    if not state.injected and fault != 'double_entry_close':
                        state.injected = True
                        successor()
                        state.committed.set()
                        if fault == 'lost_reply':
                            ws.close()
                            return
                        # The other injected transport failures happen on the worker side.
                        continue
                response = {'result': result}
            except RuntimeStoreError as exc:
                response = {'error': {'message': str(exc)}}
            ws.send(json.dumps({'id': request['id'], **response}))

    real_send = worker._OwnerLink.send
    real_receive = worker._OwnerLink.receive

    def send(link, request_id, message):
        request = json.loads(message)
        link.test_method = request['method']
        if request['method'] == 'worker.persist':
            state.sends.append(request['params'])
            if fault == 'double_entry_close' and state.entry_closes < 2:
                state.entry_closes += 1
                if state.entry_closes == 1:
                    successor()
                raise ConnectionClosed(None, None)
        real_send(link, request_id, message)
        if request['method'] == 'worker.persist' and fault == 'chained_write' and len(state.sends) == 1:
            assert state.committed.wait(5)
            raise ConnectionClosed(None, None) from BrokenPipeError('owner received the frame')

    def receive(link, timeout):
        if link.test_method == 'worker.persist' and fault == 'receipt_timeout' and len(state.sends) == 1:
            assert state.committed.wait(5)
            raise TimeoutError('worker_receipt_timeout')
        return real_receive(link, timeout)

    monkeypatch.setattr(worker._OwnerLink, 'send', send)
    monkeypatch.setattr(worker._OwnerLink, 'receive', receive)

    def compute(*args, **kwargs):
        state.computes += 1
        rows = [{'role': 'assistant', 'content': 'computed once'}]
        assert state.stores[0].append_messages_batch('s', rows) == 1
        assert rows[0]['_row_id'] > 0
        return True, 'output', 'computed once', None

    monkeypatch.setattr(scheduler, 'run_job', compute)
    try:
        with serve(handler, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            endpoint = SimpleNamespace(api_origin=f'http://127.0.0.1:{server.socket.getsockname()[1]}',
                                       control_home=None)
            monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                                lambda *args, **kwargs: SimpleNamespace(state='ready', endpoint=endpoint))
            monkeypatch.setattr(discovery, 'query_identify', lambda *args, **kwargs: {'pid': -1})
            monkeypatch.setattr(gateway_client, '_session_ticket', lambda *args, **kwargs: 'test-ticket')
            try:
                assert detached_authority.run_detached(
                    {'id': 'inert'}, {'session_id': 's'}, {'profile_id': str(tmp_path), 'generation': 0},
                    {}, lambda: None, execution_id='inert', extra_prompt=None,
                    cancel_event=None) == (True, 'output', 'computed once', None)
                assert state.computes == 1
                assert [row['content'] for row in db.get_messages('s')] == ['computed once']
                assert db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 1
                store = state.stores[0]
                assert store.scope['epoch'] == state.epoch == 2 and store.failure is None
                assert json.loads(store.path.read_text(encoding='utf-8-sig'))['pending'] == []
                identities = [{key: params[key] for key in
                               ('execution_id', 'session_id', 'generation', 'sequence', 'operation', 'payload')}
                              for params in state.sends]
                assert len(identities) == (3 if fault == 'double_entry_close' else 2)
                assert all(identity == identities[0] for identity in identities)
                methods = [method for method, _ in state.requests]
                assert methods == (['worker.register', 'worker.adopt', 'worker.adopt', 'worker.persist']
                    if fault == 'double_entry_close' else
                    ['worker.register', 'worker.adopt', 'worker.persist', 'worker.adopt', 'worker.persist'])
                assert len(state.results) == (1 if fault == 'double_entry_close' else 2)
                assert all(result == state.results[0] for result in state.results)
            finally:
                server.shutdown()
                serving.join(5)
    finally:
        for store in state.stores:
            store._outbox_owner.close()
        db.close()
