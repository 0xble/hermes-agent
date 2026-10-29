"""A reconnect must publish its manifest without waiting for list_changed."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import model_tools
from tools import mcp_tool
from tools.mcp_tool_registration import _register_server_tools
from tools.mcp_tool_scope import _server_key
from tools.registry import ToolRegistry, session_tool_scope


@pytest.mark.asyncio
async def test_reconnect_refreshes_registered_manifest_and_fresh_definitions(monkeypatch):
    registry = ToolRegistry()
    monkeypatch.setattr("tools.registry.registry", registry)
    monkeypatch.setattr(model_tools, "registry", registry)
    monkeypatch.setattr(model_tools, "_tool_defs_cache", {})

    server = mcp_tool.MCPServerTask("manifest_srv")
    monkeypatch.setitem(mcp_tool._servers, _server_key(server.name), server)
    server._tools = [
        SimpleNamespace(name="keep", description="Wait 15 seconds.", inputSchema={"type": "object"}),
        SimpleNamespace(name="removed", description="Old tool", inputSchema={"type": "object"}),
    ]
    server.session = SimpleNamespace(list_tools=AsyncMock())
    server._registered_tool_names = _register_server_tools(server.name, server, {})

    def definitions():
        return {
            tool["function"]["name"]: tool["function"]
            for tool in model_tools.get_tool_definitions(
                enabled_toolsets=[server.name], quiet_mode=True, skip_tool_search_assembly=True,
            )
        }

    keep_name = "mcp__manifest_srv__keep"
    removed_name = "mcp__manifest_srv__removed"
    previous = definitions()
    assert previous[keep_name]["description"] == "Wait 15 seconds."
    assert removed_name in previous

    new_schema = {
        "type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"],
    }
    server.session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[
        SimpleNamespace(name="keep", description="Wait 1 minute.", inputSchema=new_schema),
    ])))
    # Clean reconnects clear readiness but retain their registry entries.
    server._ready.clear()
    await server._discover_tools()

    current = registry.get_schema(keep_name)
    assert current is not None
    assert current["description"] == "Wait 1 minute."
    assert current["parameters"] == new_schema
    assert registry.get_schema(removed_name) is None
    fresh = definitions()
    assert fresh[keep_name]["description"] == "Wait 1 minute."
    assert fresh[keep_name]["parameters"] == new_schema
    assert removed_name not in fresh
    assert previous[keep_name]["description"] == "Wait 15 seconds."
    server.session.list_tools.assert_awaited_once()


@pytest.mark.asyncio
async def test_editor_reconnect_preserves_frozen_manifest(monkeypatch):
    registry = ToolRegistry()
    monkeypatch.setattr("tools.registry.registry", registry)
    server = mcp_tool.MCPServerTask("editor_manifest")
    with session_tool_scope("editor-session:reconnect"):
        monkeypatch.setitem(mcp_tool._servers, _server_key(server.name), server)
        server._tools = [
            SimpleNamespace(name="keep", description="Selected schema", inputSchema={"type": "object"}),
            SimpleNamespace(name="removed", description="Selected tool", inputSchema={"type": "object"}),
        ]
        server.session = SimpleNamespace(list_tools=AsyncMock())
        server._registered_tool_names = _register_server_tools(server.name, server, {})
        names = list(server._registered_tool_names)
        schemas = {name: registry.get_schema(name) for name in names}
        frozen = getattr(server, "_editor_frozen_manifest", None)
        assert frozen is not None

        server.session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[
            SimpleNamespace(name="keep", description="Changed schema", inputSchema={"type": "string"}),
            SimpleNamespace(name="added", description="Unselected tool", inputSchema={"type": "object"}),
        ])))
        server._ready.clear()
        await server._discover_tools()

        assert getattr(server, "_editor_frozen_manifest", None) == frozen
        assert server._registered_tool_names == names
        assert {name: registry.get_schema(name) for name in names} == schemas
        assert registry.get_schema("mcp__editor_manifest__added") is None
        server.session.list_tools.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_manifest_registration_preserves_existing_tools(monkeypatch):
    registry = ToolRegistry()
    monkeypatch.setattr("tools.registry.registry", registry)
    server = mcp_tool.MCPServerTask("atomic_manifest")
    monkeypatch.setitem(mcp_tool._servers, _server_key(server.name), server)
    server._tools = [
        SimpleNamespace(name="removed", description="Still callable", inputSchema={"type": "object"}),
    ]
    server.session = SimpleNamespace(list_tools=AsyncMock())
    server._registered_tool_names = _register_server_tools(server.name, server, {})
    names = list(server._registered_tool_names)
    schemas = {name: registry.get_schema(name) for name in names}
    tools = server._tools
    server.session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[
        SimpleNamespace(name="added", description="Rejected manifest", inputSchema={"type": "object"}),
    ])))

    def reject_registration(*args):
        raise RuntimeError("registration rejected")

    monkeypatch.setattr("tools.mcp_tool_registration._register_server_tools", reject_registration)
    with pytest.raises(RuntimeError, match="registration rejected"):
        await server._refresh_tools()

    assert server._registered_tool_names == names
    assert {name: registry.get_schema(name) for name in names} == schemas
    assert registry.get_schema("mcp__atomic_manifest__added") is None
    assert server._tools is tools
