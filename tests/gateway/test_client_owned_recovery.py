"""Finite owners must not certify or rewrite a prior daemon's recovery state."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("client_owned", [True, False])
async def test_successful_finite_turn_preserves_daemon_restart_counters(tmp_path, client_owned):
    from gateway.run_shutdown import GatewayShutdownMixin

    path = tmp_path / "restart-counters.json"
    original = b'{"completed": 2, "prior-crash": 1}'
    path.write_bytes(original)
    runner = SimpleNamespace(_client_owned=client_owned, _stuck_loop_counts_path=lambda: path)
    await GatewayShutdownMixin._clear_restart_failure_count(runner, "completed")
    if client_owned:
        assert path.read_bytes() == original
    else:
        assert json.loads(path.read_bytes()) == {"prior-crash": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_clean", [False, True])
async def test_client_owned_exit_preserves_prior_recovery_state(tmp_path, monkeypatch, prior_clean):
    from gateway import run as facade
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource

    monkeypatch.setattr(facade, "_hermes_home", tmp_path)
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", Mock())
    monkeypatch.setattr(facade.GatewayRunner, "_start_mcp_discovery", Mock())
    monkeypatch.setattr(facade.GatewayRunner, "_await_mcp_discovery", AsyncMock())
    monkeypatch.setattr(facade.GatewayRunner, "_register_launch_profile_config_hooks", Mock())
    monkeypatch.setattr(facade.GatewayRunner, "_update_runtime_status", Mock())
    monkeypatch.setattr(facade, "_shutdown_gateway_health_export", Mock())
    monkeypatch.setattr("gateway.status.flush_runtime_status_async", AsyncMock(return_value=True))
    monkeypatch.setattr("tools.process_registry.process_registry.recover_from_checkpoint", Mock(return_value=0))
    runner = await asyncio.to_thread(
        facade.GatewayRunner, GatewayConfig(sessions_dir=tmp_path / "sessions"))
    runner._client_owned = True
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="prior-crash", user_id="user")
    entry = await asyncio.to_thread(runner.session_store.get_or_create_session, source)
    token = await asyncio.to_thread(runner.session_store.mark_turn_active, entry.session_key)
    receipt = tmp_path / ".clean_shutdown"
    if prior_clean:
        receipt.write_bytes(b"prior-daemon-receipt")
        receipt_mtime = receipt.stat().st_mtime_ns
    counts = runner._stuck_loop_counts_path()
    counts.write_bytes(b'{"prior-crash": 2}')
    try:
        assert await runner.start()
        # An interrupted finite turn must not increment/prune daemon restart counters.
        ctx = facade.GatewayRunner._StopContext(deferred_count=lambda: 0, started_at=0,
                                              timed_out=False, active_agents={entry.session_key: object()})
        await runner._stop_persist_exit_state(ctx)
        assert receipt.exists() is prior_clean
        if prior_clean:
            assert receipt.read_bytes() == b"prior-daemon-receipt"
            assert receipt.stat().st_mtime_ns == receipt_mtime
        assert counts.read_bytes() == b'{"prior-crash": 2}'
        preserved = await asyncio.to_thread(runner.session_store.get_or_create_session, source)
        assert preserved.active_turn_token == token
        if not prior_clean:
            # Exercise the next daemon's real clean-vs-unclean decision, not just file absence.
            runner._recover_unclean_sessions = AsyncMock(return_value=(1, 0))
            runner._recover_secondary_process_checkpoints = Mock(return_value=0)
            runner._suspend_stuck_loop_sessions = Mock(return_value=0)
            runner._consume_clean_shutdown_marker = AsyncMock(side_effect=AssertionError("crash hidden"))
            await runner._start_recover_previous_run()
            runner._recover_unclean_sessions.assert_awaited_once()
            runner._consume_clean_shutdown_marker.assert_not_awaited()
    finally:
        await runner.stop()
