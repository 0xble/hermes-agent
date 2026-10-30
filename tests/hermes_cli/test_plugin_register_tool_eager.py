"""``ctx.register_tool(eager=True)`` keeps a real plugin's tool on the direct surface under tool search."""

import hermes_yaml as yaml

from hermes_cli import plugins as plugins_mod
from hermes_cli.plugins import PluginManager


def _schema(name):
    return {"name": name, "description": f"Plugin tool {name}", "parameters": {"type": "object", "properties": {}}}


def test_eager_plugin_tool_stays_direct_while_its_sibling_defers(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes_test"
    plugin_dir = hermes_home / "plugins" / "eager_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump({"name": "eager_plugin"}), encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        f"    ctx.register_tool(name='eager_status', toolset='plugin_eager_plugin', schema={_schema('eager_status')!r},\n"
        "                      handler=lambda args, **kw: '{}', eager=True)\n"
        f"    ctx.register_tool(name='lazy_report', toolset='plugin_eager_plugin', schema={_schema('lazy_report')!r},\n"
        "                      handler=lambda args, **kw: '{}')\n",
        encoding="utf-8",
    )
    (hermes_home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["eager_plugin"]},
        "tools": {"tool_search": {"enabled": "on"}},
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)

    from model_tools import get_tool_definitions

    names = [t["function"]["name"] for t in get_tool_definitions(
        enabled_toolsets=["terminal", "plugin_eager_plugin"], quiet_mode=True)]
    assert "eager_status" in names
    assert "lazy_report" not in names
    assert "tool_search" in names
