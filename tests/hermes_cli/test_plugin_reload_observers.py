"""Observer delivery and strict startup retain availability during plugin reloads."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hermes_cli import lifecycle, plugins


@pytest.mark.parametrize("hook", ["on_session_reset", "agent_loop_stopped", "on_human_input_request"])
@pytest.mark.asyncio
async def test_sync_loop_observers_snapshot_reload_without_waiting(monkeypatch, hook):
    manager = plugins.PluginManager()
    manager._discovered = True
    seen = []
    manager._hooks[hook] = [lambda **kwargs: seen.append(kwargs["session_id"])]
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", None)
    monkeypatch.setattr(plugins, "_join_background_discovery", lambda: pytest.fail("joined on loop"))
    monkeypatch.setattr(plugins, "_await_foreground_discovery", lambda _: pytest.fail("locked on loop"))
    lifecycle.invoke_hook(hook, session_id="reload-session")
    assert seen == ["reload-session"]


@pytest.mark.asyncio
async def test_pre_command_is_reload_snapshot_observer_not_a_policy_gate(monkeypatch):
    from gateway.run_inbound import GatewayInboundMixin
    from gateway.config import Platform
    manager = plugins.PluginManager()
    manager._discovered = True
    seen = []
    manager._hooks["pre_command"] = [lambda **kwargs: seen.append(kwargs) or {"action": "block"}]
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", None)
    monkeypatch.setattr(plugins, "_delivery_manager", lambda: pytest.fail("observer used policy barrier"))
    runner = GatewayInboundMixin()
    runner.hooks = SimpleNamespace(emit_collect=AsyncMock(return_value=[]))
    event = SimpleNamespace(get_command_args=lambda: "arg")
    source = SimpleNamespace(platform=Platform.TELEGRAM, user_id="user")
    assert await runner._hm_command_hooks(event, source, "session", "reset", "new") == (False, None, None)
    assert seen[0]["command"] == "new"
    assert seen[0]["args_raw"] == "arg"


@pytest.mark.asyncio
async def test_telegram_reload_probe_does_not_fail_update_claim(monkeypatch):
    from plugins.platforms.telegram.adapter import TelegramAdapter
    manager = plugins.PluginManager()
    manager._discovered = True
    manager._hooks["gateway_platform_event"] = [lambda **_: None]
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", None)
    monkeypatch.setattr("hermes_cli.observability.handles_hook", lambda _: False)
    monkeypatch.setattr(plugins, "_join_background_discovery", lambda: pytest.fail("joined on loop"))
    adapter = object.__new__(TelegramAdapter)
    from gateway.config import Platform
    adapter.platform = Platform.TELEGRAM
    adapter._platform_event_handler = AsyncMock()
    adapter._normalize_platform_event = Mock(return_value={"event_type": "message_edited"})
    adapter._source_for_platform_event_auth = Mock(return_value="source")
    adapter._accept_update = Mock()
    adapter._fail_update_preparation = Mock()
    update = SimpleNamespace(update_id=123)
    await adapter._on_platform_update(update, None)
    adapter._fail_update_preparation.assert_not_called()
    adapter._platform_event_handler.assert_awaited_once()


def test_strict_startup_waits_beyond_thirty_seconds_with_progress(monkeypatch, caplog):
    manager = plugins.PluginManager()
    manager._discovered = True
    waits = []
    class Sweep:
        elapsed = 0
        def is_alive(self):
            return self.elapsed < 50
        def join(self, timeout):
            waits.append(timeout)
            self.elapsed += timeout
            if not self.is_alive():
                manager._discovery_complete = True
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", Sweep())
    with caplog.at_level("INFO"):
        plugins.discover_plugins(require_complete=True)
    assert sum(waits) >= 50
    assert "Waiting for background plugin discovery" in caplog.text
    assert manager._discovery_complete


def test_strict_startup_refuses_worker_that_died_incomplete(monkeypatch):
    manager = plugins.PluginManager()
    manager._discovered = True
    class Sweep:
        alive = True
        def is_alive(self):
            return self.alive
        def join(self, timeout):
            self.alive = False
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", Sweep())
    with pytest.raises(plugins.PluginDiscoveryIncomplete, match="discovery"):
        plugins.discover_plugins(require_complete=True)
