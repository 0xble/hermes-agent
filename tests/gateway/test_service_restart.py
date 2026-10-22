"""Service reloads must drain before launchd's non-extendable SIGTERM clock starts."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.control_socket import GatewayControlServer
from gateway.service_restart import make_service_reload_handler
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("budget,finishes_at,expected_wait,remaining", [
    (0.3, 0.1, 0.1, False),
    (0.3, None, 0.3, True),
    (0, None, 0, True),
])
def test_updater_reload_waits_before_bootout_and_freezes_admission(
    tmp_path, monkeypatch, budget, finishes_at, expected_wait, remaining,
):
    from gateway import run_shutdown as shutdown
    from hermes_cli import gateway, gateway_launchd
    from tools import async_delegation

    async def scenario():
        runner, _ = make_restart_runner()
        runner._restart_after_turn_timeout = 0
        runner._restart_delegation_timeout = budget
        runner.stop = AsyncMock()
        runner._hm_is_registered_command = lambda event: False
        runner._session_key_for_source = lambda source: "new-turn"
        runner._preserve_drain_event = MagicMock()
        live = [{"delegation_id": "child", "status": "running"}]
        monkeypatch.setattr(async_delegation, "active_count", lambda: len(live))
        monkeypatch.setattr(async_delegation, "active_records", lambda: list(live))
        clock = SimpleNamespace(now=0.0)
        advance = asyncio.Event()
        bootouts = []

        async def tick(delay):
            await advance.wait()
            clock.now = round(clock.now + delay, 6)
            if finishes_at is not None and clock.now >= finishes_at:
                live.clear()
            await asyncio.sleep(0)

        monkeypatch.setattr(shutdown, "asyncio", SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: clock.now),
            sleep=tick, create_task=asyncio.create_task,
        ))
        monkeypatch.setattr(gateway, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: tmp_path / "gateway.plist")
        monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.hermes.gateway")
        monkeypatch.setattr(gateway, "_launchd_domain", lambda: "gui/501")
        monkeypatch.setattr("gateway.status.get_running_pid", lambda: os.getpid())
        monkeypatch.setattr(gateway_launchd, "_spawn_deferred_launchd_reload",
                            lambda **kwargs: bootouts.append((clock.now, bool(live))) or True)
        loop = asyncio.get_running_loop()
        server = GatewayControlServer(home=tmp_path, verb_handlers={
            "reload-service": make_service_reload_handler(runner, loop),
        })
        assert await server.start()
        try:
            result = await asyncio.to_thread(
                gateway_launchd._reload_installed_launchd_plist, tmp_path / "gateway.plist",
            )
            assert result == "deferred"
            assert runner._draining and runner._running
            if budget:
                assert bootouts == []
            event = object()
            assert await runner._hm_dispatch_quick_and_plugin_commands(
                event, make_restart_source(), None,
            ) == (True, None, None)
            runner._preserve_drain_event.assert_called_once_with("new-turn", event)
            advance.set()
            await runner._restart_task
            assert bootouts == [(expected_wait, remaining)]
            runner.stop.assert_not_awaited()
        finally:
            advance.set()
            await server.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", [False, RuntimeError("launchd unavailable")])
def test_failed_service_reload_still_reaches_watched_shutdown(tmp_path, monkeypatch, outcome):
    from gateway.service_restart import reload_service_after_wait
    from hermes_cli import gateway, gateway_launchd

    def reload(*args, **kwargs):
        assert kwargs == {"wait_for_active_work": False}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: tmp_path / "gateway.plist")
    monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist", reload)
    runner = SimpleNamespace(stop=AsyncMock())
    asyncio.run(reload_service_after_wait(runner))
    runner.stop.assert_awaited_once_with(restart=True, service_restart=True)
