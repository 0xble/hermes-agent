"""Standby Telegram deferral and in-place promotion seams."""

import asyncio
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import gateway_slot, zero_cut_standby as zcs
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.control_socket import GatewayControlServer, query_gateway_control
from gateway.run import _start_gateway_shutdown_tail


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": True}}})
    zcs.reset_for_tests()
    yield
    zcs.reset_for_tests()
    gateway_slot.reset_gateway_slot_for_tests()


@pytest.mark.asyncio
async def test_prefilter_defers_telegram_before_adapter_creation(tmp_path):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="secret")},
        sessions_dir=tmp_path / "sessions",
    )
    runner._failed_platforms = {}
    runner._abort_startup_if_shutdown_requested = lambda *args: asyncio.sleep(0, result=False)
    runner._create_adapter = lambda *args: pytest.fail("standby must not create Telegram")
    aborted, enabled, skipped, pending = await runner._start_prefilter_platforms()
    assert not aborted
    assert enabled == 0
    assert pending == []
    assert runner._failed_platforms == {}
    assert zcs.readiness()["deferred"] == ["telegram"]


@pytest.mark.asyncio
async def test_promote_connects_once_starts_cron_and_is_idempotent(monkeypatch):
    platform = Platform.TELEGRAM
    adapter = object()
    calls = []
    runner = SimpleNamespace(
        adapters={},
        _standby_deferred={(None, platform): object()},
        _create_adapter=lambda p, c: adapter,
        _wire_adapter_handlers=lambda a: calls.append("wire"),
        _connect_initial_adapter_with_timeout=lambda a, p: asyncio.sleep(0, result=True),
        _publish_primary_adapter=lambda p, a: (calls.append("publish"), runner.adapters.__setitem__(p, a)),
        _start_prime_session_db_after_ready=lambda: asyncio.sleep(0, result=calls.append("prime")),
        _start_finish_wiring=lambda count: asyncio.sleep(0, result=calls.append("finish")),
        _start_spawn_background_watchers=lambda: calls.append("watchers"),
        _safe_adapter_disconnect=lambda *args: asyncio.sleep(0),
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: ("stop", "provider", "cron", "house"))
    zcs.defer_platform(runner, platform, runner._standby_deferred[(None, platform)])
    zcs.mark_ready()
    first = await zcs.promote(runner)
    second = await zcs.promote(runner)
    assert first == {"promoted": True, "already": False, "failed": []}
    assert second == {"promoted": True, "already": True}
    assert runner.adapters[platform] is adapter
    assert runner._zero_cut_cron_handles == ("stop", "provider", "cron", "house")
    assert calls.count("publish") == 1
    assert calls.index("prime") < calls.index("publish")  # shared store opened before serving
    assert zcs.is_standby() is False


@pytest.mark.asyncio
async def test_promote_queues_failed_primary_connect_for_reconnect(monkeypatch):
    platform = Platform.TELEGRAM
    config = object()
    adapter = object()
    runner = SimpleNamespace(
        adapters={},
        _failed_platforms={},
        _standby_deferred={},
        _create_adapter=lambda p, c: adapter,
        _wire_adapter_handlers=lambda a: None,
        _connect_initial_adapter_with_timeout=lambda a, p: asyncio.sleep(0, result=False),
        _startup_retry_entry=lambda p, a, c: {"adapter": a, "config": c},
        _publish_primary_adapter=lambda p, a: pytest.fail("failed connect must not publish"),
        _start_prime_session_db_after_ready=lambda: asyncio.sleep(0),
        _start_finish_wiring=lambda count: asyncio.sleep(0),
        _start_spawn_background_watchers=lambda: None,
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
    zcs.defer_platform(runner, platform, config)
    zcs.mark_ready()
    result = await zcs.promote(runner)
    assert result == {"promoted": True, "already": False, "failed": ["telegram"]}
    assert runner._failed_platforms[platform] == {"adapter": adapter, "config": config}
    assert runner.adapters == {}


def test_promote_verb_over_real_socket_refuses_until_ready(monkeypatch):
    home = Path(tempfile.mkdtemp(prefix="hgw-", dir="/tmp"))
    runner = SimpleNamespace(
        adapters={}, _standby_deferred={},
        _start_prime_session_db_after_ready=lambda: asyncio.sleep(0),
        _start_finish_wiring=lambda count: asyncio.sleep(0),
        _start_spawn_background_watchers=lambda: None,
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)

    async def scenario():
        loop = asyncio.get_running_loop()
        verb = lambda: asyncio.run_coroutine_threadsafe(zcs.promote(runner), loop).result()
        server = GatewayControlServer(home, verb_handlers={"promote": verb})
        assert await server.start()

        def ask():
            return query_gateway_control(home, "promote", slot="b")
        try:
            before = await loop.run_in_executor(None, ask)
            zcs.mark_ready()
            first = await loop.run_in_executor(None, ask)
            again = await loop.run_in_executor(None, ask)
            return before, first, again
        finally:
            await server.stop()

    try:
        before, first, again = asyncio.run(scenario())
    finally:
        shutil.rmtree(home, ignore_errors=True)
    assert before is not None and first is not None and again is not None
    assert before["promoted"] is False and before["refused"] == "not_ready"
    assert first["promoted"] is True and first["already"] is False
    assert again["promoted"] is True and again["already"] is True


def test_promote_refused_on_active_slot(monkeypatch):
    gateway_slot.reset_gateway_slot_for_tests()
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "a")
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": True}}})
    zcs.mark_ready()
    assert asyncio.run(zcs.promote(SimpleNamespace())) == {"promoted": False, "refused": "not_standby"}


@pytest.mark.asyncio
async def test_flag_off_slot_b_creates_telegram_as_today(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner

    gateway_slot.reset_gateway_slot_for_tests()
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": False}}})
    assert zcs.is_standby() is False
    created = []
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="secret")},
        sessions_dir=tmp_path / "sessions",
    )
    runner._failed_platforms = {}
    runner._abort_startup_if_shutdown_requested = lambda *args: asyncio.sleep(0, result=False)
    runner._create_adapter = lambda platform, config: created.append(platform)
    await runner._start_prefilter_platforms()
    assert created == [Platform.TELEGRAM]
    assert zcs.readiness()["deferred"] == []


@pytest.mark.asyncio
async def test_shutdown_tail_accepts_none_handles(monkeypatch):
    runner = SimpleNamespace(_zero_cut_cron_handles=(None, None, None, None), config=None)
    monkeypatch.setattr("gateway.run._settle_mcp_discovery", lambda r: asyncio.sleep(0))
    monkeypatch.setattr("gateway.run._shutdown_mcp_servers_nonblocking", lambda config=None: asyncio.sleep(0))
    monkeypatch.setattr("gateway.run._resolve_gateway_exit_verdict", lambda r, s: True)
    monkeypatch.setattr("gateway.run._best_effort", lambda *args, **kwargs: None)
    planned = SimpleNamespace(set=lambda: None, join=lambda timeout: None)
    assert await _start_gateway_shutdown_tail(
        runner, None, None, None, None, None, planned, planned, [False]
    ) is True


def test_promotion_does_not_spawn_a_second_reconnect_watcher():
    from gateway.run import GatewayRunner

    spawned, supervised = [], []
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._failed_platforms = {}
    runner._reconnect_watcher_task = None

    class LiveTask:
        def done(self):
            return False

    def spawn_reconnect():
        spawned.append("reconnect")
        runner._reconnect_watcher_task = LiveTask()

    runner._spawn_reconnect_watcher = spawn_reconnect
    runner._spawn_supervised = lambda fn, name, **kw: supervised.append(name)
    runner._scale_to_zero_should_arm = lambda: False
    runner._log_scale_to_zero_not_armed_reason = lambda: None
    runner._start_spawn_background_watchers()  # standby: reconnect watcher only
    assert spawned == ["reconnect"] and supervised == []
    zcs.mark_ready()
    zcs._promoted = True  # what promote() sets before spawning the deferred watchers
    runner._start_spawn_background_watchers()
    assert spawned == ["reconnect"]
    assert "drain_control_watcher" in supervised


@pytest.mark.asyncio
async def test_promote_survives_connect_raise_and_queues_secondary(monkeypatch):
    platform = Platform.TELEGRAM
    adapter = object()
    queued = []

    async def boom(a, p):
        raise RuntimeError("network down")

    runner = SimpleNamespace(
        adapters={},
        _failed_platforms={},
        _profile_failed_platforms={},
        _standby_deferred={},
        _create_adapter=lambda p, c: adapter,
        _configure_profile_adapter=lambda a, prof, p: None,
        _connect_initial_adapter_with_timeout=boom,
        _safe_adapter_disconnect=lambda a, p: asyncio.sleep(0),
        _run_secondary_profile_reconnect=lambda prof, p, a: asyncio.sleep(0, result=queued.append((prof, p, a))),
        _retain_background_task=lambda task: task,
        _start_prime_session_db_after_ready=lambda: asyncio.sleep(0),
        _start_finish_wiring=lambda count: asyncio.sleep(0),
        _start_spawn_background_watchers=lambda: None,
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
    zcs.defer_platform(runner, platform, object(), profile="work")
    zcs.mark_ready()
    result = await zcs.promote(runner)
    assert result == {"promoted": True, "already": False, "failed": ["work:telegram"]}
    task = runner._profile_failed_platforms["work"][platform]
    await task
    assert queued == [("work", platform, adapter)]
    assert zcs.is_standby() is False
