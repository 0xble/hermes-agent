"""Startup regressions for state.db work on the gateway readiness path."""

import asyncio
import logging
import threading
import time
from types import SimpleNamespace

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
        runner.config.platforms[Platform.RELAY] = PlatformConfig(
            enabled=True, extra={"relay_url": "https://relay.example.test"}
        )

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
async def test_reconnect_watcher_waits_until_resume_scheduling_signal(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._reconnect_spool_tasks = {}
    runner._reconnect_resume_events = {}
    calls = []

    async def recover(platform):
        calls.append(platform)

    runner._recover_spool_after_reconnect = recover
    runner._retain_background_task = lambda task: task

    resume_scheduled = runner._start_reconnect_spool_recovery(Platform.TELEGRAM)
    assert not resume_scheduled.is_set()
    await asyncio.sleep(0)
    assert calls == [Platform.TELEGRAM]
    await resume_scheduled.wait()
    assert resume_scheduled.is_set()


@pytest.mark.asyncio
async def test_finish_wiring_discovers_mcp_before_restore_gate(monkeypatch):
    import gateway.run as gateway_run

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._mcp_discovery_ready = asyncio.Event()
    runner._startup_restore_in_progress = True
    order = []

    async def post_connect(_connected_count):
        order.append("post_connect")

    async def boot_sends(**_kwargs):
        order.append("boot_sends")

    async def discover(_config):
        order.append(("mcp", runner._startup_restore_in_progress, runner._mcp_discovery_ready.is_set()))

    async def no_candidates(*_args, **_kwargs):
        return []

    async def finish_restore():
        order.append("restore")

    monkeypatch.setattr(gateway_run, "_planned_restart_notification_pending", lambda: False)
    monkeypatch.setattr(gateway_run, "_restart_notification_pending", lambda: False)
    monkeypatch.setattr(gateway_run, "_discover_gateway_mcp_tools", discover)
    monkeypatch.setattr(
        "gateway.run_pending_recovery.recover_pending_shutdown_flush",
        lambda *args, **kwargs: 0,
    )
    runner._start_post_connect_services = post_connect
    runner._await_startup_boot_sends = boot_sends
    runner._resume_pending_candidates_async = no_candidates
    runner._schedule_resume_pending_sessions = lambda **_kwargs: order.append("schedule")
    runner._finish_startup_restore = finish_restore
    runner._schedule_auto_resume_delegations = lambda: order.append("delegations")
    runner._send_session_db_warning_notifications = no_candidates

    from tools.process_registry import process_registry
    monkeypatch.setattr(process_registry, "pending_watchers", [])
    await runner._start_finish_wiring(1)

    assert order.index(("mcp", True, False)) < order.index("restore")
    assert runner._mcp_discovery_ready.is_set()


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
    assert not marker.exists()
    assert "continuing in degraded mode" in caplog.text

    recovered = []

    async def recover_unclean():
        recovered.append(True)
        return 1, 0

    runner._recover_unclean_sessions = recover_unclean
    await runner._start_recover_previous_run()
    assert recovered == [True]


async def _raise_marker_cleanup(marker_path):
    del marker_path
    raise OSError("state store unavailable")


def test_state_db_maintenance_runs_on_first_and_every_60th_housekeeping_tick(monkeypatch):
    import gateway.run as gateway_run

    calls = []

    class SixtyOneTickStop:
        def __init__(self):
            self.ticks = 0

        def is_set(self):
            return self.ticks >= 61

        def wait(self, timeout=None):
            del timeout
            self.ticks += 1
            return False

    monkeypatch.setattr(
        gateway_run,
        "_housekeeping_state_db_maintenance",
        lambda launch=None: calls.append(launch),
    )
    gateway_run._start_gateway_housekeeping(
        SixtyOneTickStop(), interval=0, runner=SimpleNamespace(config=GatewayConfig(), adapters={}),
    )
    assert len(calls) == 2


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
