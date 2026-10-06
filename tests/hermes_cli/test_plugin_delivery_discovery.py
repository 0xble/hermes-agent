"""Regression tests for plugin delivery during asynchronous startup."""

from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _reset_discovery_barrier_state(monkeypatch):
    import hermes_cli.plugins as plugins

    monkeypatch.setattr(plugins, "_background_discovery_thread", None)
    monkeypatch.setattr(plugins, "_background_discovery_incomplete", False)
    monkeypatch.setattr(plugins, "_background_discovery_warning_emitted", False)


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


def _inflight_discovery(monkeypatch):
    """A real worker thread that registers `pre_llm_call` late while `_discovered` is already True
    (discover_and_load() publishes the flag before loading)."""
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


def test_background_discovery_barrier_waits_past_legacy_cap_for_success(monkeypatch):
    """A successful load simulated past the old 30-second cap still completes before its outer deadline."""
    import hermes_cli.plugins as plugins

    worker = threading.Thread(target=lambda: time.sleep(0.08), name="plugin-discovery", daemon=True)
    monkeypatch.setattr(plugins, "_background_discovery_thread", worker)
    monkeypatch.setattr(plugins, "_resolve_plugin_load_timeout", lambda: 0.01)
    monkeypatch.setattr(plugins, "_DISCOVERY_BARRIER_MIN_SECS", 0.02)
    monkeypatch.setattr(plugins, "_DISCOVERY_BARRIER_SLACK_SECS", 0.2)
    worker.start()

    plugins._join_background_discovery()

    assert not worker.is_alive()
    assert plugins._discovery_incomplete() is False


def test_hung_disabled_deadline_fails_closed_without_rewaiting(monkeypatch, caplog):
    """A disabled per-plugin deadline still has a hard outer barrier and never exposes partial hooks."""
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    manager._discovered = True
    manager._discovery_current_plugin = "hung-plugin"
    fired = []

    def callback(**_kwargs):
        fired.append(True)
        return "partial"

    manager._hooks["pre_llm_call"] = [callback]
    manager._middleware["before_tool"] = [callback]
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    import hermes_cli.config as config
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"plugins": {"load_timeout_seconds": 0}})
    monkeypatch.setattr(plugins, "_DISCOVERY_BARRIER_DISABLED_CAP_SECS", 0.03)

    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="plugin-discovery", daemon=True)
    monkeypatch.setattr(plugins, "_background_discovery_thread", worker)
    worker.start()

    with caplog.at_level("WARNING", logger="hermes_cli.plugins"):
        started = time.monotonic()
        plugins._join_background_discovery()
        elapsed = time.monotonic() - started
        assert plugins.invoke_hook("pre_llm_call") == []
        assert plugins.invoke_middleware("before_tool") == []
        assert plugins.has_hook("pre_llm_call") is False
        assert plugins.iter_hook_callbacks("pre_llm_call") == ()

    assert elapsed < 0.3
    assert plugins._discovery_incomplete() is True
    assert fired == []
    assert "incomplete" in caplog.text

    barrier_calls = []
    monkeypatch.setattr(plugins, "_discovery_barrier_timeout", lambda: barrier_calls.append(True) or 0.03)
    second_started = time.monotonic()
    plugins._join_background_discovery()
    assert time.monotonic() - second_started < 0.05
    assert barrier_calls == []

    release.set()
    worker.join(1)


def test_late_background_completion_reopens_hook_delivery(monkeypatch):
    """A worker that finishes after the barrier clears the closed state and resumes hooks."""
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    started = threading.Event()
    release = threading.Event()

    def discover_and_load(force=False):
        started.set()
        release.wait(1)
        manager._hooks["pre_llm_call"] = [lambda **_kwargs: "late"]
        manager._discovered = True

    manager.discover_and_load = discover_and_load
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_persist_plugin_toolset_keys", lambda: None)
    import hermes_cli.config as config
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"plugins": {"load_timeout_seconds": 0}})
    monkeypatch.setattr(plugins, "_DISCOVERY_BARRIER_DISABLED_CAP_SECS", 0.03)

    plugins.start_background_plugin_discovery()
    assert started.wait(1)
    plugins._join_background_discovery()
    assert plugins._discovery_incomplete() is True
    assert plugins.invoke_hook("pre_llm_call") == []

    release.set()
    worker = plugins._background_discovery_thread
    assert worker is not None
    worker.join(1)
    assert plugins._discovery_incomplete() is False
    assert plugins.invoke_hook("pre_llm_call") == ["late"]
