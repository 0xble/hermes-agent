"""Discovery transport loss is retriable, malformed or unauthorized peers are not."""
from types import SimpleNamespace

import pytest

from hermes_cli.gateway_runtime import discover_gateway_endpoint
from tests.agent.test_worker_rpc_connection_reuse import owner
from tests.hermes_cli.test_gateway_runtime_discovery import _ready_owner_payload


@pytest.mark.parametrize('failure', [ConnectionResetError, BrokenPipeError])
def test_worker_recovers_when_discovery_identify_loses_owner(owner, tmp_path, monkeypatch, failure):
    from agent import runtime_session_store as worker
    from hermes_cli import gateway_runtime, gateway_runtime_discovery as discovery

    calls, waits = [], []
    payload = _ready_owner_payload(tmp_path)
    payload['pid'] = -1
    payload['api_origin'] = f'http://127.0.0.1:{owner.relay.port}'

    def identify(home, *, timeout):
        calls.append(home)
        if len(calls) == 1:
            raise failure('owner exits during discovery identify')
        return payload

    monkeypatch.setattr(gateway_runtime, '_served_by_multiplexer', lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', discover_gateway_endpoint)
    monkeypatch.setattr(discovery, 'query_identify', identify)
    monkeypatch.setattr(worker, 'time', SimpleNamespace(monotonic=lambda: 0.0, sleep=waits.append))
    rpc = worker.WorkerRPC(tmp_path)
    try:
        assert rpc('worker.persist') == {'n': 1}
        assert len(calls) == 3 and waits == [0.1]
        assert owner.requests == ['worker.persist']
        assert owner.connections == owner.tickets == 1
    finally:
        rpc.close()


@pytest.mark.parametrize('reason', [None, 'invalid_runtime_state', 'foreign_stale_lock'])
def test_worker_retries_transient_owner_state(owner, tmp_path, monkeypatch, reason):
    from agent import runtime_session_store as worker
    from hermes_cli import gateway_runtime, gateway_runtime_discovery as discovery
    from gateway import runtime_ownership

    calls, waits = [], []
    payload = _ready_owner_payload(tmp_path)
    payload['pid'] = -1
    payload['api_origin'] = f'http://127.0.0.1:{owner.relay.port}'

    def identify(home, *, timeout):
        calls.append(home)
        if len(calls) == 1:
            if reason is None:
                raise FileNotFoundError('control endpoint not published')
            if reason == 'invalid_runtime_state':
                return dict(payload, state='replacing')
            raise discovery.DiscoveryError(reason)
        return payload

    monkeypatch.setattr(gateway_runtime, '_served_by_multiplexer', lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', discover_gateway_endpoint)
    monkeypatch.setattr(discovery, 'query_identify', identify)
    monkeypatch.setattr(discovery, 'missing_owner_state', lambda home: 'inaccessible')
    monkeypatch.setattr(runtime_ownership, 'unreadable_lock_recovery', lambda lock: None)
    monkeypatch.setattr(worker, 'time', SimpleNamespace(monotonic=lambda: 0.0, sleep=waits.append))
    rpc = worker.WorkerRPC(tmp_path)
    try:
        assert rpc('worker.persist') == {'n': 1}
        assert len(calls) == 3 and waits == [0.1]
        assert owner.requests == ['worker.persist'] and owner.tickets == 1
    finally:
        rpc.close()


def test_worker_protocol_mismatch_is_terminal(owner, tmp_path, monkeypatch):
    from agent import runtime_session_store as worker
    from hermes_cli import gateway_runtime, gateway_runtime_discovery as discovery

    payload = dict(_ready_owner_payload(tmp_path), runtime_protocol=2)
    waits = []
    monkeypatch.setattr(gateway_runtime, '_served_by_multiplexer', lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', discover_gateway_endpoint)
    monkeypatch.setattr(discovery, 'query_identify', lambda *args, **kwargs: payload)
    monkeypatch.setattr(worker, 'time', SimpleNamespace(monotonic=lambda: 0.0, sleep=waits.append))
    rpc = worker.WorkerRPC(tmp_path)
    try:
        with pytest.raises(worker.WorkerPersistenceError, match='runtime_protocol'):
            rpc('worker.persist')
        assert waits == [] and owner.requests == [] and owner.tickets == 0
    finally:
        rpc.close()


@pytest.mark.parametrize('failure', [ValueError, PermissionError])
def test_worker_does_not_retry_invalid_discovery_peer(owner, tmp_path, monkeypatch, failure):
    from agent import runtime_session_store as worker
    from hermes_cli import gateway_runtime, gateway_runtime_discovery as discovery

    waits = []

    def identify(home, *, timeout):
        raise failure('not an owner transition')

    monkeypatch.setattr(gateway_runtime, '_served_by_multiplexer', lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', discover_gateway_endpoint)
    monkeypatch.setattr(discovery, 'query_identify', identify)
    monkeypatch.setattr(worker, 'time', SimpleNamespace(monotonic=lambda: 0.0, sleep=waits.append))
    rpc = worker.WorkerRPC(tmp_path)
    try:
        with pytest.raises(worker.WorkerPersistenceError, match='invalid_control_peer|authorization'):
            rpc('worker.persist')
        assert waits == [] and owner.requests == [] and owner.tickets == 0
    finally:
        rpc.close()
