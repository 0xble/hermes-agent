"""Directory plugin loads must survive concurrent changes to the module table."""

from __future__ import annotations

import types

import pytest

from hermes_cli import plugins_loader
from hermes_cli.plugins import PluginManager, PluginManifest


class _ChangingModuleTable(dict):
    """Force another import/eviction at the iteration boundary, without timing sleeps.

    Replace only plugins_loader's sys reference, not the interpreter's sys.modules:
    the plugin still executes through the real importlib loader and registers a real hook.
    """

    def __init__(self, modules, action, stale_name):
        super().__init__(modules)
        self.action = action
        self.stale_name = stale_name
        self.changed = False

    def _change(self):
        if self.changed:
            return
        self.changed = True
        if self.action == "import":
            self["concurrent_startup_import"] = types.ModuleType("concurrent_startup_import")
        else:
            self.pop(self.stale_name, None)

    def __iter__(self):
        iterator = super().__iter__()
        if self.action == "import":
            yield next(iterator)
            self._change()
            yield from iterator
        else:
            yield from iterator
            # A second loader can evict a selected key after enumeration, before deletion.
            self._change()

    def copy(self):
        snapshot = super().copy()
        self._change()
        return snapshot


@pytest.mark.parametrize("action", ["import", "evict"])
def test_directory_plugin_hook_survives_module_table_changes(tmp_path, monkeypatch, action):
    """An unrelated import must not disable the plugin and silently drop its first-turn hook."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    plugin_dir = home / "plugins" / "eviction-race"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        "    ctx.register_hook('pre_llm_call', lambda **kw: {'context': 'PLUGIN-CANARY'})\n",
        encoding="utf-8",
    )
    manifest = PluginManifest(name="eviction-race", source="user", path=str(plugin_dir))
    module_name = manager._directory_module_name(manifest)
    stale_name = module_name + ".stale"
    unrelated_name = module_name + "_other"
    modules = _ChangingModuleTable(
        {"hermes_plugins": types.ModuleType("hermes_plugins"),
         module_name: types.ModuleType(module_name),
         stale_name: types.ModuleType(stale_name),
         unrelated_name: types.ModuleType(unrelated_name)},
        action, stale_name,
    )
    monkeypatch.setattr(plugins_loader, "sys", types.SimpleNamespace(modules=modules))
    try:
        manager._load_plugin(manifest)
        loaded = manager._plugins["eviction-race"]
        assert loaded.error is None
        assert loaded.enabled
        assert modules.changed  # the competing change actually happened, on old and new code
        assert stale_name not in modules
        assert unrelated_name in modules
        assert modules[module_name] is loaded.module
        if action == "import":
            assert "concurrent_startup_import" in modules
        assert manager.invoke_hook("pre_llm_call") == [{"context": "PLUGIN-CANARY"}]
    finally:
        manager.unload()
