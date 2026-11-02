"""Disposable finite-client owners never dispatch unrelated durable background work."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_client_owned_bootstrap_does_not_start_daemon_services(monkeypatch):
    from gateway import run as facade, run_bootstrap, run_runtime
    from gateway.config import GatewayConfig
    from hermes_constants import get_hermes_home

    get_hermes_home().chmod(0o700)
    cron = Mock(side_effect=AssertionError("disposable owner started cron/housekeeping"))
    post_connect = AsyncMock(side_effect=AssertionError("disposable owner restored background work"))
    watchers = Mock(side_effect=AssertionError("disposable owner started daemon watchers"))
    recovery = AsyncMock(side_effect=AssertionError("disposable owner recovered bot work"))
    hosted = Mock(side_effect=AssertionError("disposable owner started hosted coordinators"))
    monkeypatch.setattr(run_runtime, '_recover_profile_bot_mailbox', recovery)
    monkeypatch.setattr('gateway.session_hosted_service.start_ready_hosted_services', hosted)
    monkeypatch.setattr(facade, "_start_gateway_start_cron_and_housekeeping", cron)
    monkeypatch.setattr(facade.GatewayRunner, "_start_finish_wiring", post_connect)
    monkeypatch.setattr(facade.GatewayRunner, "_start_spawn_background_watchers", watchers)
    monkeypatch.setattr(run_runtime, "wait_gateway_runtime", AsyncMock())
    mcp_ready = AsyncMock()
    monkeypatch.setattr(facade.GatewayRunner, '_await_mcp_discovery', mcp_ready)
    # Production marks the runner before start(), not a global inherited by other homes.
    import inspect
    kwargs = {"client_owned": True} if "client_owned" in inspect.signature(run_bootstrap.start_gateway).parameters else {}
    await asyncio.wait_for(run_bootstrap.start_gateway(GatewayConfig(), **kwargs), 30)
    cron.assert_not_called()
    post_connect.assert_not_called()
    watchers.assert_not_called()
    recovery.assert_not_called()
    hosted.assert_not_called()
    mcp_ready.assert_awaited_once()


@pytest.mark.asyncio
async def test_client_owned_gateway_cannot_escape_through_restart(monkeypatch):
    from types import SimpleNamespace
    from gateway.run_shutdown import GatewayShutdownMixin
    runner = SimpleNamespace(_client_owned=True)
    monkeypatch.setattr('gateway.run._resolve_hermes_bin', Mock(side_effect=AssertionError('restart escaped')))
    assert GatewayShutdownMixin.request_restart(runner, detached=True) is False
    await GatewayShutdownMixin._launch_detached_restart_command(runner)
