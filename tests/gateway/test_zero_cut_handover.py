"""Contracts for the opt-in zero-cut gateway handover seams."""

import asyncio
import threading
from types import SimpleNamespace

from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli import gateway_launchd
from gateway.config import GatewayConfig, Platform, is_zero_cut_handover_enabled
from gateway.config_loader import bridge_toplevel_keys
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.disconnect_handover import run_disconnect


def test_zero_cut_handover_defaults_off():
    assert DEFAULT_CONFIG["gateway"]["zero_cut_handover"]["enabled"] is False
    assert GatewayConfig.from_dict({}).zero_cut_handover_enabled is False
    assert GatewayConfig.from_dict(
        {"gateway": {"zero_cut_handover": {"enabled": True}}}
    ).zero_cut_handover_enabled is True


def test_raw_mapping_flag_uses_config_bool_coercion():
    assert is_zero_cut_handover_enabled({"gateway": {"zero_cut_handover": {"enabled": "false"}}}) is False
    assert is_zero_cut_handover_enabled({"zero_cut_handover": {"enabled": "true"}}) is True
    assert is_zero_cut_handover_enabled(None) is False


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
            # The bound expired: the step owner cancels the stop, so it never runs here.
            awaitable.close()
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
            awaitable.close()
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


def test_disconnect_flag_on_releases_lock_when_quota_flush_cancels(monkeypatch):
    events = []
    adapter = _make_disconnect_adapter(monkeypatch, enabled=True, events=events)
    started = threading.Event()
    unblock = threading.Event()

    class Quota:
        def flush(self):
            started.set()
            unblock.wait(1)

    setattr(adapter, "_telegram_daily_quota", Quota())

    async def scenario():
        task = asyncio.create_task(adapter.disconnect())
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        unblock.set()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert "release" in events


def test_disconnect_flag_on_inline_caller_releases_on_error_and_keeps_no_task_callback(monkeypatch):
    """The reconnect watcher awaits disconnect() inline and survives its errors; the lock must still
    be released, and nothing may stay attached to the long-lived caller task."""
    events = []
    adapter = _make_disconnect_adapter(monkeypatch, enabled=True, events=events)

    async def fail_on_lifecycle(awaitable, _timeout, step):
        if step == "lifecycle-task cancel":
            raise RuntimeError("boom")
        if asyncio.iscoroutine(awaitable):
            await awaitable
        events.append(step)
        return True

    monkeypatch.setattr(adapter, "_await_disconnect_step", fail_on_lifecycle)
    monkeypatch.setattr(
        adapter, "_collect_live_tasks",
        lambda *_: [asyncio.get_running_loop().create_future()],
    )

    async def watcher():
        host = asyncio.current_task()
        before = len(getattr(host, "_callbacks", None) or [])
        try:
            await adapter.disconnect()
        except RuntimeError:
            pass
        assert "release" in events
        assert len(getattr(host, "_callbacks", None) or []) == before

    asyncio.run(watcher())


def test_disconnect_flag_on_cancels_and_releases_when_updater_stop_times_out(monkeypatch):
    """A wedged updater.stop() is cancelled at the bound, as on the legacy path, before the lock goes."""
    events = []
    adapter = _make_disconnect_adapter(monkeypatch, enabled=True, events=events)
    stop_cancelled = []

    async def wedged_stop():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stop_cancelled.append(True)
            raise

    setattr(adapter._app, "updater", SimpleNamespace(running=True, stop=wedged_stop))
    monkeypatch.setattr(adapter, "_await_disconnect_step", TelegramAdapter._await_disconnect_step.__get__(adapter))
    monkeypatch.setattr("plugins.platforms.telegram.adapter._UPDATER_STOP_TIMEOUT", 0.05)
    monkeypatch.setattr("plugins.platforms.telegram.adapter._DISCONNECT_STEP_TIMEOUT", 0.05)

    async def scenario():
        await adapter.disconnect()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert stop_cancelled == [True]
    assert events.index("release") < events.index("app.stop")


def test_detached_disconnect_does_not_release_a_successor_lock(monkeypatch, tmp_path):
    """A same-process successor re-acquires the token lock while the old teardown still runs."""
    from gateway import status

    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path))
    old = object.__new__(TelegramAdapter)
    old.gateway_runner = SimpleNamespace(config=GatewayConfig(zero_cut_handover_enabled=True))
    old._platform_lock_scope, old._platform_lock_identity = "telegram-bot-token", "token"
    old._platform_lock_nonce = "old-nonce"
    assert status.acquire_scoped_lock("telegram-bot-token", "token", metadata={"nonce": "old-nonce"})[0]
    lock_path = status._get_scope_lock_path("telegram-bot-token", "token")

    async def scenario():
        finish = asyncio.Event()

        async def teardown(*, zero_cut_handover):
            await finish.wait()
            zero_cut_handover()

        outer = asyncio.ensure_future(run_disconnect(old, teardown))
        await asyncio.sleep(0)
        outer.cancel()  # the gateway's disconnect budget gives up
        await asyncio.sleep(0.01)
        assert status.acquire_scoped_lock(
            "telegram-bot-token", "token", metadata={"nonce": "successor-nonce"})[0]  # successor
        finish.set()
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert lock_path.exists()
    assert old._platform_lock_identity is None


def test_detached_disconnect_survives_garbage_collection_and_releases(monkeypatch, tmp_path):
    """Once the gateway stops waiting, nothing else references the teardown; it must still finish."""
    import gc

    from gateway import status

    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path))
    old = object.__new__(TelegramAdapter)
    old.gateway_runner = SimpleNamespace(config=GatewayConfig(zero_cut_handover_enabled=True))
    old._platform_lock_scope, old._platform_lock_identity = "telegram-bot-token", "token"
    old._platform_lock_nonce = "old-nonce"
    assert status.acquire_scoped_lock("telegram-bot-token", "token", metadata={"nonce": "old-nonce"})[0]
    lock_path = status._get_scope_lock_path("telegram-bot-token", "token")

    async def scenario():
        loop = asyncio.get_running_loop()

        async def teardown(*, zero_cut_handover):
            stopped = loop.create_future()  # referenced only from this frame, like updater.stop()
            loop.call_later(0.05, stopped.set_result, None)
            await stopped
            zero_cut_handover()

        outer = asyncio.ensure_future(run_disconnect(old, teardown))
        await asyncio.sleep(0)
        outer.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await outer
        del outer
        gc.collect()
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert not lock_path.exists()


def test_detached_disconnect_spares_successor_with_identical_timestamp(monkeypatch, tmp_path):
    """Two acquisitions in the same clock tick still differ by nonce, so the successor survives."""
    from gateway import status

    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(status, "_utc_now_iso", lambda: "2026-10-15T00:00:00.000000+00:00")
    old = object.__new__(TelegramAdapter)
    old.gateway_runner = SimpleNamespace(config=GatewayConfig(zero_cut_handover_enabled=True))
    old._platform_lock_scope, old._platform_lock_identity = "telegram-bot-token", "token"
    old._platform_lock_nonce = "old-nonce"
    assert status.acquire_scoped_lock("telegram-bot-token", "token", metadata={"nonce": "old-nonce"})[0]
    assert status.acquire_scoped_lock("telegram-bot-token", "token", metadata={"nonce": "new-nonce"})[0]
    lock_path = status._get_scope_lock_path("telegram-bot-token", "token")

    async def teardown(*, zero_cut_handover):
        zero_cut_handover()

    asyncio.run(run_disconnect(old, teardown))
    assert lock_path.exists()
    assert old._platform_lock_identity is None


def test_real_platform_lock_acquire_records_a_fresh_nonce_each_time(monkeypatch, tmp_path):
    """Every acquire, including a same-PID reacquire, stamps a new owner nonce on disk."""
    from gateway import status
    from plugins.platforms.telegram.disconnect_handover import _lock_owner_nonce

    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path))
    first, second = object.__new__(TelegramAdapter), object.__new__(TelegramAdapter)
    for adapter in (first, second):
        adapter.platform = Platform.TELEGRAM
        adapter._platform_lock_takeover_allowed = False
        adapter._platform_lock_takeover_attempted = False
    assert first._acquire_platform_lock("telegram-bot-token", "token", "Telegram bot token")
    assert _lock_owner_nonce(first) == first._platform_lock_nonce
    assert second._acquire_platform_lock("telegram-bot-token", "token", "Telegram bot token")
    assert first._platform_lock_nonce != second._platform_lock_nonce
    assert _lock_owner_nonce(first) == second._platform_lock_nonce
    assert status._get_scope_lock_path("telegram-bot-token", "token").exists()


def test_detached_disconnect_spares_same_adapter_reacquire(monkeypatch, tmp_path):
    """If this very adapter re-acquires before its old teardown finishes, the new lock survives."""
    from gateway import status

    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path))
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.gateway_runner = SimpleNamespace(config=GatewayConfig(zero_cut_handover_enabled=True))
    adapter._platform_lock_takeover_allowed = False
    adapter._platform_lock_takeover_attempted = False
    assert adapter._acquire_platform_lock("telegram-bot-token", "token", "Telegram bot token")
    lock_path = status._get_scope_lock_path("telegram-bot-token", "token")

    async def scenario():
        finish = asyncio.Event()

        async def teardown(*, zero_cut_handover):
            await finish.wait()
            zero_cut_handover()

        outer = asyncio.ensure_future(run_disconnect(adapter, teardown))
        await asyncio.sleep(0)
        outer.cancel()  # the gateway's disconnect budget gives up
        await asyncio.sleep(0.01)
        assert adapter._acquire_platform_lock("telegram-bot-token", "token", "Telegram bot token")
        finish.set()
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert lock_path.exists()
    assert adapter._platform_lock_identity == "token"
