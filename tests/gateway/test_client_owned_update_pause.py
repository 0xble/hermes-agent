"""Update pause must not claim ownership of a finite client's lifetime."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["bootstrap", "legacy"])
@pytest.mark.parametrize("client_owned, accepted", [(True, False), (False, True), (False, False)])
async def test_pause_reports_client_ownership_without_claiming_a_drain(
    monkeypatch, entrypoint, client_owned, accepted
):
    from gateway import run, run_bootstrap
    from gateway.control_socket import pause_gateway_for_update
    from gateway.run_shutdown import GatewayShutdownMixin
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.chmod(0o700)
    restart = Mock(return_value=accepted)
    runner = SimpleNamespace(
        _client_owned=client_owned,
        session_runtime_descriptor={"client_owned": client_owned},
        session_ticket_store=None,
        request_restart=restart,
    )
    if client_owned:
        runner.request_restart = lambda **kw: GatewayShutdownMixin.request_restart(runner, **kw)
    monkeypatch.setattr("hermes_cli.gateway._get_restart_drain_timeout", lambda: 12.5)
    start = (run_bootstrap if entrypoint == "bootstrap" else run)._start_gateway_start_control_socket
    server = await start(runner)
    assert server is not None
    try:
        answer = await asyncio.to_thread(pause_gateway_for_update, home)
    finally:
        await server.stop()
    if client_owned:
        assert answer == {"pausing": False, "client_owned": True}
        assert not getattr(runner, "_draining", False)
    else:
        assert answer == {"pausing": accepted, "already_stopping": not accepted,
                          "pid": os.getpid(), "drain_timeout": 12.5}
        restart.assert_called_once_with(detached=False, via_service=True)
