"""Bad config must not be indistinguishable from removing every MCP server."""
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("text", [
    "model_presets: {p: {provider: openrouter, model: m}}\ndelegation: {model_preset: p, model: inline}\n",
    "mcp_servers: [broken\n",
    "mcp_servers: not-a-mapping\n",
])
def test_reconcile_keeps_live_lazy_and_connecting_servers_on_invalid_config(tmp_path, monkeypatch, caplog, text):
    from tools import mcp_tool as core, mcp_tool_discovery as discovery
    from gateway import run_profile_reconcile as housekeeping

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text(text)
    scope = core._mcp_registry_scope()
    live, lazy = object(), {"command": "existing"}
    monkeypatch.setattr(core, "_servers", {"keep": live})
    monkeypatch.setattr(core, "_server_scope_keys", {"keep": scope, "connecting": scope})
    monkeypatch.setattr(core, "_server_connecting", {"connecting"})
    monkeypatch.setattr(core, "_lazy_server_configs", {"lazy": lazy})
    shutdown, discover = Mock(), Mock()
    monkeypatch.setattr(discovery._lifecycle, "shutdown_mcp_servers", shutdown)
    monkeypatch.setattr(discovery, "discover_mcp_tools", discover)
    monkeypatch.setattr(housekeeping, "_for_each_served_profile", lambda runner, body: body("fixture"))
    tick = housekeeping._mcp_config_reconciler()
    with caplog.at_level("ERROR"):
        tick()  # startup discovery owns the first tick
        tick()
        tick()
    assert core._servers == {"keep": live}
    assert core._server_connecting == {"connecting"}
    assert core._lazy_server_configs == {"lazy": lazy}
    shutdown.assert_not_called()
    discover.assert_not_called()
    assert sum("MCP reconcile skipped" in r.message for r in caplog.records) == 1
    # A repaired document can still intentionally remove a server. It is not a permanent hold.
    path.write_text("mcp_servers: {}\n")
    tick()
    shutdown.assert_called_once_with(scope=scope, names={"keep"})
