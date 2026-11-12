"""A goal-peer RPC losing its answer exits cleanly without claiming no mutation occurred."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from hermes_cli import gateway_client, gateway_runtime, goal_peer_command


@pytest.mark.parametrize('failed_method', ['session.resume', 'session.automation'])
@pytest.mark.parametrize('error', [TimeoutError('gateway answer timed out'), OSError('gateway transport lost')])
def test_peer_command_transport_error_is_a_clean_nonzero_exit(monkeypatch, capsys, failed_method, error):
    class Peer:
        async def rpc(self, method, **params):
            if method == failed_method:
                raise error
            return {}

    @asynccontextmanager
    async def connected(**kwargs):
        yield Peer()

    monkeypatch.setenv('HERMES_SESSION_ID', 'parent')
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda home: SimpleNamespace(endpoint=object()))
    monkeypatch.setattr(gateway_runtime, 'attachable', lambda result: True)
    monkeypatch.setattr(gateway_client, 'connect_gateway', connected)
    monkeypatch.setattr('sys.argv', ['goal-peer', 'list'])
    assert goal_peer_command.main() == 1
    captured = capsys.readouterr()
    assert str(error) in captured.err
    assert 'Traceback' not in captured.err
    assert 'no change was submitted' not in captured.err
    assert not captured.out
