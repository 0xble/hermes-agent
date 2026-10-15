"""Standby Telegram deferral and in-place promotion seams."""

import asyncio
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
        _start_finish_wiring=lambda count: asyncio.sleep(0, result=calls.append("finish")),
        _start_spawn_background_watchers=lambda: calls.append("watchers"),
        _safe_adapter_disconnect=lambda *args: asyncio.sleep(0),
    )
    monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: ("stop", "provider", "cron", "house"))
    zcs.defer_platform(runner, platform, runner._standby_deferred[(None, platform)])
    zcs.mark_ready()
    first = await zcs.promote(runner)
    second = await zcs.promote(runner)
    assert first == {"promoted": True, "already": False}
    assert second == {"promoted": True, "already": True}
    assert runner.adapters[platform] is adapter
    assert runner._zero_cut_cron_handles == ("stop", "provider", "cron", "house")
    assert calls.count("publish") == 1
    assert zcs.is_standby() is False


def test_promote_control_socket_refuses_until_ready(tmp_path):
    async def scenario():
        server = GatewayControlServer(tmp_path, verb_handlers={"promote": lambda: {"promoted": False, "refused": "not_ready"}})
        assert await server.start()
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, lambda: query_gateway_control(tmp_path, "promote", slot="b"))
        finally:
            await server.stop()

    assert asyncio.run(scenario())["refused"] == "not_ready"


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
