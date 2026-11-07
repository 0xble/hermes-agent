"""Loaded identity describes registration-time imports, independent of later disk contents."""

import asyncio
import hashlib
import os
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from hermes_cli import plugins
from hermes_cli.plugins import PluginManifest


@pytest.fixture
def identity_env(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("HERMES_SESSION_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: bundled)
    monkeypatch.setattr(plugins.PluginManager, "_scan_entry_points", lambda self: [])
    plugins._reset_plugin_managers_for_tests()
    yield tmp_path
    plugins._reset_plugin_managers_for_tests()


def _plugin(home, *, isolation="in_process", value=b"old", body=None):
    directory = home / "plugins" / "identity-probe"
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text("name: identity-probe\nversion: '1.2.3'\n")
    source = body or (
        b"from . import helper\n"
        b"def register(ctx):\n"
        b"    from . import registered\n"
        b"    def touched(*args, **kwargs):\n"
        b"        raise AssertionError('identity read dispatched plugin code')\n"
        b"    ctx.register_hook('pre_tool_call', touched)\n"
        b"    ctx.register_command('identity-command', touched)\n"
        b"def lazy():\n"
        b"    from . import lazy_module\n"
    )
    sources = {"__init__.py": source, "helper.py": b"VALUE = '" + value + b"'\n",
               "registered.py": b"REGISTERED = True\n"}
    for name, data in sources.items():
        (directory / name).write_bytes(data)
    (directory / "lazy_module.py").write_bytes(b"LAZY = True\n")
    (home / "config.yaml").write_text(
        f"plugins:\n  enabled: [identity-probe]\n  isolation: {isolation}\n")
    return directory, sources


def _digest(sources):
    return hashlib.sha256(b"".join(sources[name] for name in sorted(sources))).hexdigest()


def _row():
    return next(p for p in plugins.get_plugin_manager().list_plugins() if p["key"] == "identity-probe")


@pytest.mark.parametrize("isolation", ["in_process", "host"])
def test_loaded_sources_survive_disk_replacement_until_real_profile_reload(identity_env, monkeypatch, isolation):
    homes = [identity_env / name for name in ("a", "b")]
    first_dir, first_sources = _plugin(homes[0], isolation=isolation)
    _, second_sources = _plugin(homes[1], isolation=isolation, value=b"two")
    identities = []
    for home, sources in zip(homes, (first_sources, second_sources)):
        monkeypatch.setenv("HERMES_HOME", str(home))
        plugins.discover_plugins(force=True)
        row = _row()
        assert row["enabled"] and row["error"] is None
        identity = row["code_identity"]
        assert identity["digest"] == _digest(sources)
        assert identity["version"] == "1.2.3"
        assert identity["module_count"] == len(sources)
        assert datetime.fromisoformat(identity["loaded_at"]).utcoffset() is not None
        identities.append(identity)
    monkeypatch.setenv("HERMES_HOME", str(homes[0]))
    assert _row()["code_identity"] == identities[0]
    # Equal size and mtime exercise stale timestamp-based .pyc caches on supported reloads.
    helper = first_dir / "helper.py"
    stat = helper.stat()
    first_sources["helper.py"] = b"VALUE = 'new'\n"
    helper.write_bytes(first_sources["helper.py"])
    os.utime(helper, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert _row()["code_identity"] == identities[0]
    if isolation == "in_process":
        module = plugins.get_plugin_manager()._plugins["identity-probe"].module
        assert module.helper.VALUE == "old"
        module.lazy()
        assert _row()["code_identity"] == identities[0]  # lazy imports are outside the snapshot
    plugins.discover_plugins(force=True)
    current = _row()["code_identity"]
    assert current["digest"] == _digest(first_sources) != identities[0]["digest"]
    if isolation == "in_process":
        assert plugins.get_plugin_manager()._plugins["identity-probe"].module.helper.VALUE == "new"
    monkeypatch.setenv("HERMES_HOME", str(homes[1]))
    assert _row()["code_identity"] == identities[1]


def _snapshot(home):
    return {p.relative_to(home).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in home.rglob("*") if p.is_file()}


def test_existing_surfaces_report_identity_without_running_plugins_or_writing(identity_env, monkeypatch):
    from gateway.control_socket import build_status_payload
    from gateway.run_plugin_rewire import reload_plugins_verb
    from gateway.session_discovery import list_plugins

    home = identity_env / "profile"
    _plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    plugins.discover_plugins(force=True)
    manager = plugins.get_plugin_manager()
    broken = home / "broken"
    broken.mkdir()
    (broken / "__init__.py").write_text("def register(ctx):\n    raise ValueError('broken')\n")
    manager._load_plugin(PluginManifest(name="broken", path=str(broken), source="user"))
    disabled = plugins.LoadedPlugin(PluginManifest(name="disabled"), error="disabled via config")
    deferred = plugins.LoadedPlugin(PluginManifest(name="deferred"), enabled=True, deferred=True)
    manager._plugins.update(disabled=disabled, deferred=deferred)
    for key, enabled, error in (("broken", False, "broken"), ("disabled", False, "disabled via config"),
                                ("deferred", True, None)):
        row = next(p for p in manager.list_plugins() if p["key"] == key)
        assert row["code_identity"] is None
        assert (row["enabled"], row["error"], row["tools"], row["hooks"], row["commands"]) == (
            enabled, error, 0, 0, 0)
    assert deferred.deferred is True
    actor = SimpleNamespace(capabilities={"session:read"}, profile_id=str(home))
    authority = SimpleNamespace(profile_id=str(home))
    # A status/discovery read must not turn into a discovery sweep, even on an uninitialized manager.
    discover = plugins.discover_plugins
    monkeypatch.setattr(plugins, "discover_plugins", lambda *a, **k: pytest.fail("read triggered discovery"))
    monkeypatch.setattr(manager, "invoke_hook", lambda *a, **k: pytest.fail("read invoked hook"))
    from tools.registry import registry
    dispatch = registry.dispatch
    monkeypatch.setattr(registry, "dispatch", lambda *a, **k: pytest.fail("read dispatched tool"))
    before = _snapshot(home)
    listed = asyncio.run(list_plugins(authority, actor, {}))
    status = build_status_payload()
    identity = _row()["code_identity"]
    for payload in (listed, status):
        row = next(p for p in payload["plugins"] if p["key"] == "identity-probe")
        assert row["name"] == "identity-probe" and row["code_identity"] == identity
    assert _snapshot(home) == before
    assert not list(home.rglob("review_receipts"))
    # The existing reload answer keeps its list of names and adds compact identity rows.
    monkeypatch.setattr(plugins, "discover_plugins", discover)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr("gateway.run_plugin_rewire._reconcile_plugin_mcp_servers", lambda *a: None)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    try:
        answer = reload_plugins_verb(SimpleNamespace(adapters={}), loop)({})
        assert "identity-probe" in answer["plugins"]
        row = next(p for p in answer["plugin_details"] if p["key"] == "identity-probe")
        assert row["code_identity"] == _row()["code_identity"]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
