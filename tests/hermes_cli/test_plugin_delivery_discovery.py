"""Regression tests for plugin delivery during asynchronous startup."""

from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _reset_discovery_barrier_state(monkeypatch):
    import hermes_cli.plugins as plugins

    monkeypatch.setattr(plugins, "_background_discovery_thread", None)
    monkeypatch.setattr(plugins, "_discovery_done", threading.Event())
    monkeypatch.setattr(plugins, "_discovery_outcome", "ok")
    monkeypatch.setattr(plugins, "_barrier_timed_out", False)
    monkeypatch.setattr(plugins, "_delivery_warning_emitted", False)


def test_delivery_manager_waits_for_inflight_background_discovery(monkeypatch):
    """A discovered flag is published before loading finishes; hooks must wait for the worker."""
    import hermes_cli.plugins as plugins

    class Manager:
        _discovered = True

    manager = Manager()
    joined = []
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_join_background_discovery", lambda: joined.append(True))

    assert plugins._delivery_manager() is manager
    assert joined == [True]


def test_pre_command_hook_waits_for_inflight_discovery(monkeypatch):
    """The pre-command path must use the same barrier as every other hook consumer."""
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    manager._discovered = True
    fired = []
    started = threading.Event()

    def callback(**kwargs):
        fired.append(kwargs["command"])

    def late_registration():
        started.set()
        time.sleep(0.03)
        manager._hooks.setdefault("pre_command", []).append(callback)
        plugins._publish_discovery_outcome("ok")

    worker = threading.Thread(target=late_registration, name="plugin-discovery", daemon=True)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", worker)
    monkeypatch.setattr(plugins, "_DISCOVERY_BARRIER_MIN_SECS", 0.2)
    worker.start()
    assert started.wait(1)

    plugins.fire_pre_command_hook(
        surface="cli", command="model", alias_used="m", args_raw="--help",
    )

    worker.join(1)
    assert fired == ["model"]


def test_has_hook_and_streaming_snapshot_wait_for_inflight_discovery(monkeypatch):
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    manager._discovered = True
    callback = lambda **_kwargs: None
    started = threading.Event()

    def late_registration():
        started.set()
        time.sleep(0.03)
        manager._hooks.setdefault("pre_llm_call", []).append(callback)
        plugins._publish_discovery_outcome("ok")

    worker = threading.Thread(target=late_registration, name="plugin-discovery", daemon=True)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", worker)
    worker.start()
    assert started.wait(1)

    assert plugins.has_hook("pre_llm_call") is True
    assert callback in plugins.iter_hook_callbacks("pre_llm_call")
    worker.join(1)


def test_worker_exception_publishes_failed_and_fails_closed_without_retry(monkeypatch, caplog):
    import hermes_cli.plugins as plugins

    class Manager:
        _discovered = False

        def __init__(self):
            self.calls = 0
            self._hooks = {"pre_llm_call": [lambda **_: "partial"]}
            self._middleware = {"before_tool": [lambda **_: "partial"]}
            self._discovery_current_plugin = "broken-plugin"

        def discover_and_load(self):
            self.calls += 1
            raise RuntimeError("broken plugin")

    manager = Manager()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_persist_plugin_toolset_keys", lambda: None)

    plugins.start_background_plugin_discovery()
    worker = plugins._background_discovery_thread
    assert worker is not None
    worker.join(1)

    with caplog.at_level("WARNING", logger="hermes_cli.plugins"):
        assert plugins.invoke_hook("pre_llm_call") == []
        assert plugins.invoke_middleware("before_tool") == []
        assert plugins.render_system_prompt_sections({}) == []
        assert plugins.has_hook("pre_llm_call") is False
        assert plugins.has_middleware("before_tool") is False

    assert plugins._discovery_done.is_set()
    assert plugins._discovery_outcome == "failed"
    assert manager.calls == 1
    assert caplog.text.count("Plugin hook delivery skipped because plugin discovery failed") == 1


def test_timeout_completion_interleaving_leaves_delivery_open(monkeypatch):
    """Completion between wait return and timeout bookkeeping must not close delivery."""
    import hermes_cli.plugins as plugins

    class Event:
        def __init__(self):
            self.completed = False

        def is_set(self):
            return self.completed

        def wait(self, timeout):
            self.completed = True
            return False

        def clear(self):
            self.completed = False

        def set(self):
            self.completed = True

    class Manager:
        _discovered = True

        def has_hook(self, name):
            return name == "pre_llm_call"

        def invoke_hook(self, name, **_kwargs):
            return ["open"]

    event = Event()
    manager = Manager()
    worker = threading.Thread(target=lambda: None, name="plugin-discovery", daemon=True)
    monkeypatch.setattr(plugins, "_discovery_done", event)
    monkeypatch.setattr(plugins, "_background_discovery_thread", worker)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)

    plugins._join_background_discovery()

    assert plugins._barrier_timed_out is False
    assert plugins._discovery_incomplete() is False
    assert plugins.invoke_hook("pre_llm_call") == ["open"]


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
    assert plugins._barrier_timed_out is True
    assert fired == []
    assert "incomplete" in caplog.text
    assert "hung-plugin" in caplog.text
    assert caplog.text.count("Plugin discovery incomplete after") == 1

    barrier_calls = []
    monkeypatch.setattr(plugins, "_discovery_barrier_timeout", lambda: barrier_calls.append(True) or 0.03)
    second_started = time.monotonic()
    plugins._join_background_discovery()
    assert time.monotonic() - second_started < 0.05
    assert barrier_calls == []

    release.set()
    worker.join(1)


def test_late_background_completion_reopens_hook_delivery(monkeypatch):
    """A worker that finishes after the barrier clears delivery without a separate clear step."""
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
    assert plugins._discovery_done.is_set()
    assert plugins._discovery_outcome == "ok"
    assert plugins._discovery_incomplete() is False
    assert plugins.invoke_hook("pre_llm_call") == ["late"]


def test_normal_delivery_path_unchanged(monkeypatch):
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    manager._discovered = False
    def discover_and_load(force=False):
        manager._hooks.setdefault("pre_llm_call", []).append(lambda **_: "normal")
        manager._discovered = True

    manager.discover_and_load = discover_and_load
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)

    assert plugins.invoke_hook("pre_llm_call") == ["normal"]
    assert plugins._discovery_outcome == "ok"
    assert plugins._discovery_done.is_set()
