"""PR1 contracts for the opt-in zero-cut gateway handover seams."""

import asyncio
from types import SimpleNamespace

from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli import gateway_launchd
from gateway.config import GatewayConfig, Platform
from gateway.config_loader import bridge_toplevel_keys
from plugins.platforms.telegram.adapter import TelegramAdapter


def test_zero_cut_handover_defaults_off():
    assert DEFAULT_CONFIG["gateway"]["zero_cut_handover"]["enabled"] is False
    assert GatewayConfig.from_dict({}).zero_cut_handover_enabled is False
    assert GatewayConfig.from_dict(
        {"gateway": {"zero_cut_handover": {"enabled": True}}}
    ).zero_cut_handover_enabled is True


def test_real_yaml_gateway_section_is_bridged_before_schema_parse():
    gw_data = {}
    bridge_toplevel_keys(
        {"gateway": {"zero_cut_handover": {"enabled": True}}},
        {"zero_cut_handover": {"enabled": True}},
        gw_data,
    )
    assert GatewayConfig.from_dict(gw_data).zero_cut_handover_enabled is True


def test_launchd_slot_labels_reuse_default_label_convention(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(
        gateway_launchd,
        "_gw",
        lambda: SimpleNamespace(_profile_suffix=lambda: ""),
    )
    assert gateway_launchd.get_launchd_slot_labels() == (
        "ai.hermes.gateway",
        "ai.hermes.gateway.b",
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-zero-cut"))
    from hermes_cli import gateway
    monkeypatch.setattr(gateway_launchd, "_gw", lambda: gateway)
    active, standby = gateway_launchd.get_launchd_slot_labels()
    assert active == gateway.get_launchd_label()
    assert active.startswith("ai.hermes.gateway-")
    assert standby == f"{active}.b"


def _make_disconnect_adapter(monkeypatch, *, enabled: bool, events: list[str]):
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(zero_cut_handover_enabled=enabled)
    )
    adapter._telegram_daily_quota = None
    adapter._polling_generation = 0
    adapter._post_connect_task = None
    adapter._polling_error_task = None
    adapter._polling_progress_verifier_task = None
    adapter._polling_heartbeat_task = None
    adapter._bot_identity_refresh_task = None
    adapter._update_receipt_flush = None
    adapter._app = SimpleNamespace()
    adapter._app.running = True

    async def updater_stop():
        events.append("updater.stop")

    adapter._app.updater = SimpleNamespace(running=True, stop=updater_stop)

    async def app_stop():
        events.append("app.stop")

    async def app_shutdown():
        events.append("app.shutdown")

    adapter._app.stop = app_stop
    adapter._app.shutdown = app_shutdown
    adapter._platform_lock_identity = "token"

    monkeypatch.setattr(adapter, "_release_platform_lock", lambda: events.append("release"))
    monkeypatch.setattr(adapter, "_mark_disconnected", lambda: events.append("mark-disconnected"))
    monkeypatch.setattr(adapter, "_collect_live_tasks", lambda *_: [])
    monkeypatch.setattr(adapter, "_clear_task_attrs_except", lambda *_: None)
    monkeypatch.setattr(adapter, "_cancel_task_attr", lambda *_: asyncio.sleep(0))
    monkeypatch.setattr(adapter, "_set_status_indicator", lambda **_: asyncio.sleep(0))
    monkeypatch.setattr(adapter, "_cancel_pending_delivery_tasks", lambda: asyncio.sleep(0))

    async def bounded(awaitable, _timeout, step):
        if step == "updater.stop()":
            await awaitable
        elif asyncio.iscoroutine(awaitable):
            await awaitable
        events.append(step)
        return True

    monkeypatch.setattr(adapter, "_await_disconnect_step", bounded)
    return adapter


def test_disconnect_flag_off_keeps_lock_release_before_updater_stop(monkeypatch):
    events = []
    adapter = _make_disconnect_adapter(monkeypatch, enabled=False, events=events)

    asyncio.run(adapter.disconnect())

    assert events.index("release") < events.index("updater.stop")


def test_disconnect_flag_on_releases_lock_after_updater_stop(monkeypatch):
    events = []
    adapter = _make_disconnect_adapter(monkeypatch, enabled=True, events=events)

    asyncio.run(adapter.disconnect())

    assert events.index("updater.stop") < events.index("release")
    assert events.index("release") < events.index("app.stop")


def test_disconnect_flag_on_releases_lock_when_updater_stop_times_out(monkeypatch):
    events = []
    adapter = _make_disconnect_adapter(monkeypatch, enabled=True, events=events)

    async def bounded_timeout(awaitable, _timeout, step):
        if step == "updater.stop()":
            # The real stop task remains detached and observed, but teardown must not deadlock.
            return False
        if asyncio.iscoroutine(awaitable):
            await awaitable
        events.append(step)
        return True

    monkeypatch.setattr(adapter, "_await_disconnect_step", bounded_timeout)
    asyncio.run(adapter.disconnect())

    assert "release" in events
    assert events.index("release") < events.index("app.stop")


def test_disconnect_flag_on_releases_lock_when_outer_teardown_cancels(monkeypatch):
    events = []
    adapter = _make_disconnect_adapter(monkeypatch, enabled=True, events=events)

    async def cancel_on_updater_stop(awaitable, _timeout, step):
        if step == "updater.stop()":
            raise asyncio.CancelledError
        if asyncio.iscoroutine(awaitable):
            await awaitable
        events.append(step)
        return True

    monkeypatch.setattr(adapter, "_await_disconnect_step", cancel_on_updater_stop)
    try:
        asyncio.run(adapter.disconnect())
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("disconnect should propagate outer cancellation")

    assert "release" in events


def test_disconnect_flag_on_releases_lock_when_cancelled_before_updater(monkeypatch):
    events = []
    adapter = _make_disconnect_adapter(monkeypatch, enabled=True, events=events)

    async def cancel_before_updater(awaitable, _timeout, step):
        if step == "lifecycle-task cancel":
            raise asyncio.CancelledError
        if asyncio.iscoroutine(awaitable):
            await awaitable
        events.append(step)
        return True

    monkeypatch.setattr(adapter, "_await_disconnect_step", cancel_before_updater)
    monkeypatch.setattr(
        adapter, "_collect_live_tasks",
        lambda *_: [asyncio.get_running_loop().create_future()],
    )
    try:
        asyncio.run(adapter.disconnect())
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("disconnect should propagate outer cancellation")

    assert "release" in events


__all__ = [
    "test_zero_cut_handover_defaults_off",
    "test_launchd_slot_labels_reuse_default_label_convention",
    "test_disconnect_flag_off_keeps_lock_release_before_updater_stop",
    "test_disconnect_flag_on_releases_lock_after_updater_stop",
    "test_disconnect_flag_on_releases_lock_when_updater_stop_times_out",
    "test_disconnect_flag_on_releases_lock_when_outer_teardown_cancels",
    "test_disconnect_flag_on_releases_lock_when_cancelled_before_updater",
]
