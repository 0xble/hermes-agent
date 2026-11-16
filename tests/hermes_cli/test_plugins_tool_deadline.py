"""Plugin tools declare a bounded execution deadline through PluginContext.register_tool."""

import pytest

from hermes_cli.plugins import PluginManager


def test_register_tool_execution_timeout_extends_executor_deadline(tmp_path, monkeypatch):
    """A plugin's declared bounded budget reaches the registry and the executor's deadline."""
    from agent.tool_dispatch_helpers import _extend_tool_timeout
    from tools.registry import registry
    from hermes_cli.plugins import PluginContext, PluginManifest
    from registration_lifecycle import replacement_coordinator

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_test"))
    mgr = PluginManager()
    ctx = PluginContext(manager=mgr, manifest=PluginManifest(name="deadline_plugin", source="bundled"))

    def _schema(name):
        return {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}}

    long_name, plain_name = "deadline_long_tool", "deadline_plain_tool"
    handles = []
    try:
        handles.append(ctx.register_tool(name=long_name, toolset="deadline_plugin", schema=_schema(long_name),
                                         handler=lambda args, **kw: "l", execution_timeout=1200))
        handles.append(ctx.register_tool(name=plain_name, toolset="deadline_plugin", schema=_schema(plain_name),
                                         handler=lambda args, **kw: "p"))
        assert all(handle is not None for handle in handles)
        assert registry.get_entry(long_name, scope=mgr.scope_key).execution_timeout == 1200.0
        assert _extend_tool_timeout(long_name, 420.0) == 1200.0
        assert _extend_tool_timeout(plain_name, 420.0) == 420.0
        for bad in (0, -5, float("inf"), float("nan"), True, "1200", 10**1000):
            with pytest.raises(ValueError, match="execution_timeout"):
                ctx.register_tool(name="deadline_bad_tool", toolset="deadline_plugin",
                                  schema=_schema("deadline_bad_tool"),
                                  handler=lambda args, **kw: "b", execution_timeout=bad)
        assert registry.get_entry("deadline_bad_tool", scope=mgr.scope_key) is None
    finally:
        # The tools live in the manager's scope layer, not the process-global one: release them through
        # their registration handles so the scoped entries and replacement leases are unwound.
        for handle in reversed(handles):
            if handle is not None:
                handle.dispose()
    for name in (long_name, plain_name):
        assert registry.get_entry(name, scope=mgr.scope_key) is None
        assert ("tool", mgr.scope_key, name) not in replacement_coordinator._active
    assert not {long_name, plain_name} & mgr._plugin_tool_names
