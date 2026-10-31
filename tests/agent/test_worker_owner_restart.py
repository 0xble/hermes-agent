"""Owner transition before send waits for a successor, without losing the receipt journal."""
import json
import socket
import threading
from types import SimpleNamespace

import pytest


def test_append_survives_mid_identify_close_and_adopts_successor(tmp_path, monkeypatch):
    from websockets.sync.server import serve
    from agent import runtime_session_store as worker
    from hermes_cli import gateway_client, gateway_runtime, gateway_runtime_discovery as discovery
    from hermes_state import SessionDB
    from hermes_state_runtime import RuntimeStoreError, adopt_worker_execution, mutate_worker_execution, register_worker_execution
    from tests.fakes.runtime_owner import begin_runtime_epoch

    db = SessionDB(tmp_path / 'owner.db')
    db.create_session('s', 'cli')
    epoch = begin_runtime_epoch(db, instance_id='first')
    scope = dict(epoch=epoch, execution_id='worker', session_id='s', generation=0)
    register_worker_execution(db, **scope, kind='compute', adoption_secret='private')
    state = SimpleNamespace(interrupt=False, identifies=0, discoveries=[], tickets=[], requests=[],
                            stopping=threading.Event())

    def control():
        while not state.stopping.is_set():
            try:
                peer, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with peer:
                peer.recv(4096)
                state.identifies += 1
                if state.interrupt:
                    state.interrupt = False
                    peer.sendall(b'{"protocol":')  # the exiting owner closes mid-identify
                else:
                    peer.sendall(json.dumps({'protocol': 1, 'id': 1, 'ok': True,
                                             'result': {'pid': -1}}).encode() + b'\n')

    def handler(ws):
        for raw in ws:
            request = json.loads(raw)
            method, params = request['method'], request['params']
            state.requests.append((method, params))
            try:
                if method == 'worker.adopt':
                    result = adopt_worker_execution(db, epoch=epoch, **params, adoption_secret='private')
                else:
                    result = mutate_worker_execution(db, **params)
                response = {'result': result}
            except RuntimeStoreError as exc:
                response = {'error': {'message': str(exc)}}
            ws.send(json.dumps({'id': request['id'], **response}))

    def connect_private(home, timeout):
        return socket.create_connection(listener.getsockname(), timeout=timeout)

    def discover(home, timeout):
        state.discoveries.append(endpoint.instance_id)
        return SimpleNamespace(state='ready', endpoint=endpoint)

    def ticket(home, endpoint, *, purpose, timeout=5):
        assert purpose == 'worker-adoption'
        state.tickets.append(endpoint.instance_id)
        return f'{endpoint.instance_id}-ticket-{len(state.tickets)}'

    listener = socket.create_server(('127.0.0.1', 0))
    listener.settimeout(0.2)
    controller = threading.Thread(target=control, daemon=True)
    controller.start()
    rpc = worker.WorkerRPC(tmp_path)
    store = worker.RuntimeSessionStore(rpc, scope, tmp_path / 'outbox')
    try:
        with serve(handler, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            endpoint = SimpleNamespace(api_origin=f'http://127.0.0.1:{server.socket.getsockname()[1]}',
                                       control_home=None, instance_id='first')
            monkeypatch.setattr(discovery, 'connect_private', connect_private)
            monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', discover)
            monkeypatch.setattr(gateway_client, '_session_ticket', ticket)
            monkeypatch.setattr(worker, 'OWNER_RESTART_TIMEOUT', 5.0, raising=False)
            assert store.append_messages_batch('s', [{'role': 'user', 'content': 'before restart'}]) == 1
            rpc.close()  # the old owner's idle link has closed before the next write
            epoch = begin_runtime_epoch(db, instance_id='successor')
            endpoint = SimpleNamespace(**{**vars(endpoint), 'instance_id': 'successor'})
            state.interrupt = True
            try:
                rows = [{'role': 'assistant', 'content': 'after restart'}]
                assert store.append_messages_batch('s', rows) == 1
                assert rows[0]['_row_id'] > 0
                assert [row['content'] for row in db.get_messages('s')] == ['before restart', 'after restart']
                assert state.identifies == 3
                assert state.discoveries == ['first', 'successor', 'successor']
                assert state.tickets == ['first', 'successor']
                assert [method for method, _ in state.requests] == [
                    'worker.persist', 'worker.persist', 'worker.adopt', 'worker.persist']
                attempts = [params for method, params in state.requests if method == 'worker.persist']
                assert attempts[1]['sequence'] == attempts[2]['sequence'] == 2
                assert attempts[1]['payload'] == attempts[2]['payload']
                assert store.scope['epoch'] == epoch
                assert json.loads(store.path.read_text(encoding='utf-8-sig'))['pending'] == []
                assert db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 2
            finally:
                rpc.close()
                server.shutdown()
                serving.join(5)
    finally:
        store._outbox_owner.close()
        state.stopping.set()
        listener.close()
        controller.join(5)
        assert not controller.is_alive()
        db.close()


@pytest.mark.parametrize('failure', [
    'incomplete_control_response', 'owner_unavailable', 'draining', 'connection_closed',
    'connection_refused', 'connection_reset', 'missing_socket', 'timeout',
    'unsafe_control_peer', 'invalid_control_response', 'synchronous_self_rpc',
    'ticket_close', 'ticket_identity', 'ticket_timeout',
])
def test_absent_owner_deadline_preserves_journal_and_explains_cause(tmp_path, monkeypatch, failure):
    from websockets.exceptions import ConnectionClosed
    from agent import runtime_session_store as worker
    from agent.turn_explainers import TurnExplainersMixin
    from hermes_cli import gateway_runtime, gateway_runtime_discovery as discovery
    from hermes_state_errors import PERSISTENCE_ERROR_CAUSES, classify_persistence_error
    from hermes_state_user_copy import describe_storage_failure

    # A virtual clock proves the full production patience without wall-clock sleeps.
    clock = SimpleNamespace(now=0.0, waits=[])

    def wait(seconds):
        clock.waits.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(worker, 'time', SimpleNamespace(monotonic=lambda: clock.now, sleep=wait), raising=False)
    endpoint = SimpleNamespace(control_home=None)
    calls = []

    def discover(home, timeout):
        calls.append(timeout)
        state = failure if failure in ('owner_unavailable', 'draining') else 'ready'
        return SimpleNamespace(state=state, endpoint=endpoint if state == 'ready' else None)

    def identify(home, timeout):
        if failure.startswith('ticket_'):
            return {'pid': -1}
        errors = {
            'connection_closed': ConnectionClosed(None, None),
            'connection_refused': ConnectionRefusedError('control socket refused'),
            'connection_reset': ConnectionResetError('control socket reset'),
            'missing_socket': FileNotFoundError('control socket gone'),
            'timeout': TimeoutError('control identify timeout'),
        }
        raise errors.get(failure, discovery.DiscoveryError(failure))

    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', discover)
    monkeypatch.setattr(discovery, 'query_identify', identify)
    from hermes_cli import gateway_client

    def ticket(*args, **kwargs):
        errors = {'ticket_close': 'incomplete_control_response',
                  'ticket_identity': 'Gateway bootstrap identity changed; retry launch',
                  'ticket_timeout': 'Gateway bootstrap timed out'}
        raise gateway_client.GatewayClientError(errors[failure])

    monkeypatch.setattr(gateway_client, '_session_ticket', ticket)
    rpc = worker.WorkerRPC(tmp_path)
    store = worker.RuntimeSessionStore(rpc, {'session_id': 's'}, tmp_path / 'outbox')
    try:
        if failure in ('unsafe_control_peer', 'invalid_control_response', 'synchronous_self_rpc'):
            with pytest.raises((RuntimeError, discovery.DiscoveryError), match=failure):
                store.append_messages_batch('s', [{'role': 'user', 'content': 'kept'}])
            assert clock.now == 0 and clock.waits == []
            assert len(calls) == 1
            return
        with pytest.raises(RuntimeError, match='owner_restart') as failed:
            store.append_messages_batch('s', [{'role': 'user', 'content': 'kept'}])
        assert classify_persistence_error(failed.value) == 'owner_restart'
        assert classify_persistence_error(store.failure) == 'owner_restart'
        assert 'owner_restart' in PERSISTENCE_ERROR_CAUSES
        assert clock.now == pytest.approx(90.0)
        assert clock.waits and len(calls) > 1 and all(0 < budget <= 5 for budget in calls)
        pending = json.loads(store.path.read_text(encoding='utf-8-sig'))['pending']
        assert pending[0]['payload']['messages'][0]['content'] == 'kept'
        assert pending[0]['sequence'] == 1
        explanation = TurnExplainersMixin._format_turn_completion_explanation(
            'session_persistence_failed', classify_persistence_error(failed.value))
        assert 'gateway restarted or became unavailable' in explanation
        assert 'local journal' in explanation and 'Do not resend' in explanation
        assert 'out of room' not in explanation and 'holding the database' not in explanation
        description = describe_storage_failure(store.failure)
        assert description.code == 'storage_owner_restart'
        assert 'local journal' in description.action
    finally:
        store._outbox_owner.close()
        rpc.close()
