"""Regression tests for plugin delivery during asynchronous startup."""

from __future__ import annotations

import pytest


def test_delivery_manager_waits_for_inflight_background_discovery(monkeypatch):
    """A discovered flag is published before loading finishes; hooks must wait for the worker."""
    import hermes_cli.plugins as plugins

    class Manager:
        _discovered = _discovery_complete = True

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


def test_tool_dispatch_blocks_until_discovery_finishes_then_honors_veto(monkeypatch):
    import json
    import threading

    import hermes_cli.plugins as plugins
    import model_tools

    manager = plugins.PluginManager()
    entered, release = threading.Event(), threading.Event()
    executions, veto_calls = [], []

    def veto(**kwargs):
        veto_calls.append(kwargs["tool_name"])
        return {"action": "block", "message": "configured veto"}

    def load():
        entered.set()
        assert release.wait(10)
        manager._hooks.setdefault("pre_tool_call", []).append(veto)

    def execute(name, args, **kwargs):
        executions.append(name)
        return json.dumps({"ok": True})

    def dispatch():
        return model_tools.handle_function_call(
            "discovery_probe", {}, skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )

    monkeypatch.setattr(manager, "_discover_and_load_inner", load)
    monkeypatch.setattr(manager, "_refresh_secret_sources_after_discovery", lambda: None)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(model_tools.registry, "dispatch", execute)
    join = plugins._join_background_discovery
    monkeypatch.setattr(plugins, "_join_background_discovery", lambda: join(timeout=0))
    worker = threading.Thread(target=manager.discover_and_load, daemon=True)
    monkeypatch.setattr(plugins, "_background_discovery_thread", worker)
    worker.start()
    try:
        assert entered.wait(5)
        result = dispatch()
        assert veto_calls == []
        assert executions == [], "discovery timeout must not authorize tool execution"
        assert "discovery" in json.loads(result)["error"].lower()
        with pytest.raises(RuntimeError, match="discovery"):
            plugins.invoke_hook("post_tool_call")
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert "configured veto" in json.loads(dispatch())["error"]
    assert veto_calls == ["discovery_probe"]
    assert executions == []
    manager._hooks["pre_tool_call"].clear()
    assert json.loads(dispatch()) == {"ok": True}
    assert executions == ["discovery_probe"]


def _inflight_discovery(monkeypatch):
    """A real worker thread that registers `pre_llm_call` late while `_discovered` is already True
    (discover_and_load() publishes the flag before loading)."""
    import threading
    import time

    import hermes_cli.plugins as plugins
    from tests.plugin_helpers import mark_plugin_discovery_started

    manager = plugins.PluginManager()
    mark_plugin_discovery_started(manager)

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


def test_concurrent_first_delivery_waits_for_sibling_foreground_discovery(monkeypatch):
    """Concurrent tool workers both lazily discover: the one that loses the sweep lock must wait
    for completion instead of failing its tool, and still see hooks registered by that sweep."""
    import threading

    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    entered, release = threading.Event(), threading.Event()
    seen = []

    def load():
        entered.set()
        assert release.wait(10)
        manager._hooks.setdefault("post_tool_call", []).append(lambda **kw: seen.append(kw) or "ran")

    monkeypatch.setattr(manager, "_discover_and_load_inner", load)
    monkeypatch.setattr(manager, "_refresh_secret_sources_after_discovery", lambda: None)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    first = threading.Thread(target=lambda: plugins.invoke_hook("post_tool_call", n=1), daemon=True)
    first.start()
    assert entered.wait(5)
    result = {}

    def sibling():
        try:
            result["value"] = plugins.invoke_hook("post_tool_call", n=2)
        except Exception as exc:  # pragma: no cover - the regression
            result["error"] = exc

    worker = threading.Thread(target=sibling, daemon=True)
    worker.start()
    worker.join(0.3)
    assert worker.is_alive(), "sibling must wait for the in-flight sweep, not deliver or fail early"
    release.set()
    worker.join(5)
    first.join(5)
    assert result == {"value": ["ran"]}


def test_sweep_thread_reentry_still_refuses_partial_registry(monkeypatch):
    """Re-entrant delivery from inside the sweep (register()-time hook) stays fail closed."""
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    outcome = {}

    def load():
        try:
            plugins.invoke_hook("post_tool_call")
        except RuntimeError as exc:
            outcome["error"] = str(exc)

    monkeypatch.setattr(manager, "_discover_and_load_inner", load)
    monkeypatch.setattr(manager, "_refresh_secret_sources_after_discovery", lambda: None)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    manager.discover_and_load()
    assert "discovery incomplete" in outcome["error"].lower()
    assert manager._discovery_complete is True
