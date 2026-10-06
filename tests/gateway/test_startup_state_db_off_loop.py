"""Startup regressions for state.db work on the gateway readiness path."""

import asyncio
import logging
import threading
import time

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
import gateway.run_startup as run_startup


@pytest.mark.asyncio
async def test_relay_registration_precedes_relay_adapter_prefilter(monkeypatch, tmp_path):
    """Relay registration must happen before the relay-only adapter list is built."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("GATEWAY_RELAY_URL", "https://relay.example.test")
    order = []

    def fake_register():
        order.append("register")

    async def fake_prefilter(self):
        assert Platform.RELAY in self.config.platforms
        order.append(("prefilter", Platform.RELAY))
        return False, 1, [], []

    async def fake_recover(self):
        assert order == ["register", ("prefilter", Platform.RELAY)]

    monkeypatch.setattr(
        run_startup.GatewayStartupMixin,
        "_start_register_plugins_relay_hooks",
        staticmethod(fake_register),
    )
    monkeypatch.setattr(run_startup.GatewayStartupMixin, "_start_prefilter_platforms", fake_prefilter)
    monkeypatch.setattr(run_startup.GatewayStartupMixin, "_start_recover_previous_run", fake_recover)

    async def no_abort(self, *args):
        return False

    monkeypatch.setattr(run_startup.GatewayStartupMixin, "_abort_startup_if_shutdown_requested", no_abort)
    monkeypatch.setattr(GatewayRunner, "_start_log_startup_environment", _async_noop)
    monkeypatch.setattr(GatewayRunner, "_start_free_tier_bootstrap", staticmethod(lambda: None))
    monkeypatch.setattr(GatewayRunner, "_start_install_faulthandler", lambda self: None)
    monkeypatch.setattr(GatewayRunner, "_start_check_access_policy", lambda self: False)
    monkeypatch.setattr(GatewayRunner, "_start_startup_warmup", lambda self: None)
    monkeypatch.setattr(GatewayRunner, "_start_connect_pending", _async_return_empty)
    monkeypatch.setattr(GatewayRunner, "_start_aggregate_connect_results", _async_return_one)
    monkeypatch.setattr(GatewayRunner, "_start_secondary_profiles", _async_return_one_pair)
    monkeypatch.setattr(GatewayRunner, "_start_handle_no_connections", lambda self, *args: False)
    monkeypatch.setattr(GatewayRunner, "_start_prime_session_db_after_ready", _async_noop)
    monkeypatch.setattr(GatewayRunner, "_wire_teams_pipeline_runtime", lambda self: None)
    monkeypatch.setattr(GatewayRunner, "_install_plugin_message_injector", lambda self: None)
    monkeypatch.setattr(GatewayRunner, "_update_runtime_status", lambda self, *args: None)
    monkeypatch.setattr(GatewayRunner, "_start_finish_wiring", _async_noop)
    monkeypatch.setattr(GatewayRunner, "_start_spawn_background_watchers", lambda self: None)
    monkeypatch.setattr(GatewayRunner, "_start_flush_runtime_status", staticmethod(_async_noop))
    monkeypatch.setattr(GatewayRunner, "_subscribe_plugin_rewire", lambda self, *args: None)

    config = GatewayConfig(
        platforms={Platform.RELAY: PlatformConfig(enabled=True, extra={"relay_url": "https://relay.example.test"})},
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)
    runner.hooks.discover_and_load = lambda: None
    assert await runner.start() is True


async def _async_noop(*args, **kwargs):
    del args, kwargs


async def _async_return_empty(*args, **kwargs):
    del args, kwargs
    return []


async def _async_return_one(*args, **kwargs):
    del args, kwargs
    return 1


async def _async_return_one_pair(*args, **kwargs):
    del args, kwargs
    return False, 1


@pytest.mark.asyncio
async def test_startup_recovery_snapshot_does_not_block_gateway_loop():
    """A blocking recovery read must run in a worker, not on the event-loop thread."""
    runner = object.__new__(GatewayRunner)
    started = threading.Event()

    def blocking_snapshot(*args, **kwargs):
        del args, kwargs
        started.set()
        time.sleep(0.2)
        return ["complete"]

    runner._resume_pending_candidates = blocking_snapshot
    recovery = asyncio.create_task(runner._resume_pending_candidates_async())
    await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=0.5)

    probe = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(probe.set_result, True)
    assert await asyncio.wait_for(probe, timeout=0.05)
    assert await recovery == ["complete"]


@pytest.mark.asyncio
async def test_clean_marker_failure_keeps_connected_startup_degraded(monkeypatch, tmp_path, caplog):
    """A late clean-marker failure must not strand connected adapters by aborting startup."""
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("clean", encoding="utf-8")
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = object.__new__(GatewayRunner)
    runner._startup_recovery_degraded = False
    runner._consume_clean_shutdown_marker = _raise_marker_cleanup
    runner._suspend_stuck_loop_sessions = lambda: 0

    from tools.process_registry import process_registry
    monkeypatch.setattr(process_registry, "recover_from_checkpoint", lambda: 0)
    monkeypatch.setattr(runner, "_recover_secondary_process_checkpoints", lambda registry: 0)

    with caplog.at_level(logging.ERROR):
        await runner._start_recover_previous_run()

    assert runner._startup_recovery_degraded is True
    assert runner._serving_state() == "degraded"
    assert marker.exists()
    assert "continuing in degraded mode" in caplog.text


async def _raise_marker_cleanup(marker_path):
    del marker_path
    raise OSError("state store unavailable")


def test_gateway_constructor_does_not_open_or_maintain_state_db(monkeypatch, tmp_path):
    """Constructor setup must not contend on state.db before adapters can connect."""
    calls = []
    monkeypatch.setattr(
        GatewayRunner,
        "_open_session_db_for_active_scope",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "gateway.run._housekeeping_state_db_maintenance",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
        sessions_dir=tmp_path / "sessions",
    )

    GatewayRunner(config)

    assert calls == []
