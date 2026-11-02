"""Only exact children spawned for a finite client are granted teardown authority."""
from unittest.mock import Mock
import pytest


def test_late_spawn_after_cancelled_client_immediately_loses_lease():
    from hermes_cli.gateway_client_lifetime import ClientGatewayLease
    lease = ClientGatewayLease()
    lease.close()
    child = Mock()
    lease.own(child)
    child.stdin.close.assert_called_once()
    child.kill.assert_not_called()


def test_lease_closes_pipe_and_reaps_own_children_only():
    from hermes_cli.gateway_client_lifetime import ClientGatewayLease
    lease = ClientGatewayLease()
    child = Mock()
    lease.own(child)
    lease.close()
    lease.close()
    child.stdin.close.assert_called_once()
    child.wait.assert_called_once()
    child.kill.assert_not_called()


def test_owned_spawn_uses_private_pipe_and_marker(monkeypatch, tmp_path):
    import subprocess
    from hermes_cli.gateway_client_lifetime import ClientGatewayLease
    from hermes_cli.gateway_runtime_start import spawn_unmanaged_gateway
    from unittest.mock import patch

    lease = ClientGatewayLease()
    with patch('hermes_cli.gateway_runtime_start.subprocess.Popen') as popen:
        child = popen.return_value
        spawn_unmanaged_gateway(tmp_path, deadline=__import__('time').monotonic() + 10, client_lease=lease)
        argv, = popen.call_args.args
        kwargs = popen.call_args.kwargs
        assert '--client-owned' in argv and '--idle-exit' not in argv
        assert kwargs['stdin'] == subprocess.PIPE
        assert kwargs['close_fds'] is True
        lease.close()
        child.stdin.close.assert_called_once()


def test_reused_runtime_is_never_owned(monkeypatch, tmp_path):
    from hermes_cli import gateway_runtime as runtime
    from hermes_cli.gateway_client_lifetime import ClientGatewayLease
    ready = runtime.GatewayDiscovery('ready', Mock())
    monkeypatch.setattr(runtime, '_observe_replacing_stale', lambda *a: ready)
    spawn = Mock(side_effect=AssertionError('borrowed runtime must not spawn'))
    monkeypatch.setattr('hermes_cli.gateway_runtime_start.spawn_unmanaged_gateway', spawn)
    lease = ClientGatewayLease()
    assert runtime.ensure_gateway_runtime(tmp_path, client_lease=lease) is ready
    lease.close()
    spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['ensure', 'ticket', 'connect'])
async def test_every_attach_failure_releases_spawned_lease(monkeypatch, tmp_path, failure):
    from types import SimpleNamespace
    from hermes_cli import gateway_client, gateway_runtime as runtime
    child = Mock()
    endpoint = SimpleNamespace(api_origin='http://127.0.0.1:1', profile_id=str(tmp_path),
                               instance_id='test', runtime_protocol=1)

    def ensure(home, **kwargs):
        kwargs['client_lease'].own(child)
        if failure == 'ensure':
            raise RuntimeError('ensure failed')
        return runtime.GatewayDiscovery('ready', endpoint)

    def ticket(*a, **kw):
        if failure == 'ticket':
            raise RuntimeError('ticket failed')
        return 'fake'

    def connect(*a, **kw):
        raise OSError('connect failed')

    monkeypatch.setattr(runtime, 'ensure_gateway_runtime', ensure)
    monkeypatch.setattr(gateway_client, '_session_ticket', ticket)
    monkeypatch.setattr('websockets.asyncio.client.connect', connect)
    with pytest.raises((RuntimeError, gateway_client.GatewayClientError), match=failure):
        async with gateway_client.connect_gateway(one_shot=True):
            pytest.fail('failed attach yielded a client')
    child.stdin.close.assert_called_once()
    child.wait.assert_called_once()
