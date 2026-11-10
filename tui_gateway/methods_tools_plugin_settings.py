"""Topical helpers extracted from tui_gateway/methods_tools.py."""
from __future__ import annotations

def _plugins_settings(rid, params):
    """Write manifest-declared settings (``values`` = ``{key: value}``) through the same writer as
    ``ctx.set_config``; secrets are refused here (the client stores them via the ``.env`` route)."""
    from tui_gateway.server import (Path, _err, _ok, _plugin_rows, _portable_server_names, _tools_mod)
    key = (params.get("key") or params.get("name") or "").strip()
    values = params.get("values")
    if not key or not isinstance(values, dict):
        return _err(rid, 4019, "plugins.settings requires a 'key' and a 'values' mapping")
    pc = _tools_mod("hermes_cli.plugins_cmd")
    found = pc._find_plugin_entry(key)
    if found is None:
        return _err(rid, 4020, f"plugin '{key}' not found")
    _name, _version, _desc, _source, plugin_dir, canonical = found
    try:
        written = _tools_mod("hermes_cli.plugins_settings").save_plugin_settings(
            canonical, Path(str(plugin_dir)) if plugin_dir else None, values)
    except (ValueError, PermissionError) as e:
        return _err(rid, 4021, str(e))
    row = next((r for r in _plugin_rows() if r["key"] == canonical), None)
    # A Desktop half applies its own preferences (a status-bar chip on/off) without polling.
    events = _tools_mod("hermes_cli.plugin_events")
    if events._PLUGIN_ID_RE.match(canonical):  # category keys (``image_gen/fal``) form no event name
        events.broadcast_plugin_event(canonical, "settings.changed", {"written": written})
    # A secret the form just stored may feed the plugin's MCP server env (``${MESHY_API_KEY}``): bounce
    # only servers whose resolved launch inputs changed, off the RPC thread (an npx start takes seconds).
    if plugin_dir and pc._is_portable_plugin_dir(plugin_dir):
        servers = _portable_server_names(Path(str(plugin_dir)), canonical)
        if servers:
            from agent.memory_provider import spawn_context_thread  # keeps this request's profile scope
            discovery = _tools_mod("tools.mcp_tool_discovery")
            spawn_context_thread(discovery.restart_mcp_servers_with_new_inputs, args=(servers,),
                                 name=f"plugin-settings-mcp-{canonical}").start()
    return _ok(rid, {"ok": True, "name": canonical, "written": written, "plugin": row})


def _plugins_onboarding(rid, params):
    """Catalog plugins curated for the onboarding card that this OS runs, each with its app state."""
    from tui_gateway.server import (_ok, _tools_mod)
    return _ok(rid, {"onboarding": _tools_mod("hermes_cli.plugin_catalog_presence").onboarding_entries()})


def _plugins_presence(rid, params):
    """Named catalog plugins this OS runs, in the order asked, each with its app state and disclosure."""
    from tui_gateway.server import (_ok, _tools_mod)
    return _ok(rid, {"presence": _tools_mod("hermes_cli.plugin_catalog_presence").presence_for(params.get("names") or [])})
