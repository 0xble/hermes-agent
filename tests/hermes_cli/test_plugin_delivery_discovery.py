"""Regression tests for plugin delivery during asynchronous startup."""

from __future__ import annotations


def test_delivery_manager_waits_for_inflight_background_discovery(monkeypatch):
    """A discovered flag is published before loading finishes; hooks must wait for the worker."""
    import hermes_cli.plugins as plugins

    class Manager:
        _discovered = True

    manager = Manager()
    joined = []
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        plugins,
        "_join_background_discovery",
        lambda: joined.append(True),
    )

    assert plugins._delivery_manager() is manager
    assert joined == [True]


def test_delivery_refuses_partial_hooks_after_discovery_timeout(monkeypatch):
    import threading
    import pytest
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    entered, release = threading.Event(), threading.Event()
    dispatched = []
    def load():
        manager._hooks.setdefault("pre_tool_call", []).append(lambda **kw: dispatched.append(True))
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(manager, "_discover_and_load_inner", load)
    monkeypatch.setattr(manager, "_refresh_secret_sources_after_discovery", lambda: None)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    join = plugins._join_background_discovery
    monkeypatch.setattr(plugins, "_join_background_discovery", lambda: join(timeout=0))
    worker = threading.Thread(target=manager.discover_and_load, daemon=True)
    monkeypatch.setattr(plugins, "_background_discovery_thread", worker)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(RuntimeError, match="discovery"):
            plugins.invoke_hook("pre_tool_call")
        assert dispatched == []
    finally:
        release.set()
        worker.join(5)
    plugins.invoke_hook("pre_tool_call")
    assert dispatched == [True]


def _inflight_discovery(monkeypatch):
    """A real worker thread that registers `pre_llm_call` late while `_discovered` is already True
    (discover_and_load() publishes the flag before loading)."""
    import threading
    import time

    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    manager._discovered = True

    def callback(**_kwargs):
        return None

    started = threading.Event()

    def late_registration():
        started.set()
        time.sleep(0.3)
        manager._hooks.setdefault("pre_llm_call", []).append(callback)
        manager._discovery_complete = True

    worker = threading.Thread(target=late_registration, name="plugin-discovery", daemon=True)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", worker)
    worker.start()
    assert started.wait(5)
    return plugins, callback, worker


def test_has_hook_sees_hook_registered_by_inflight_discovery(monkeypatch):
    plugins, _callback, worker = _inflight_discovery(monkeypatch)
    assert plugins.has_hook("pre_llm_call") is True
    worker.join(5)


def test_streaming_hook_snapshot_sees_hook_registered_by_inflight_discovery(monkeypatch):
    plugins, callback, worker = _inflight_discovery(monkeypatch)
    assert callback in plugins.iter_hook_callbacks("pre_llm_call")
    worker.join(5)
