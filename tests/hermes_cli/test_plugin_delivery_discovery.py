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


def test_background_discovery_barrier_has_no_partial_registry_timeout(monkeypatch):
    """The delivery barrier must not return while a slow worker still owns discovery."""
    import hermes_cli.plugins as plugins

    class SlowWorker:
        def __init__(self):
            self.join_calls = []
            self._alive = True

        def is_alive(self):
            return self._alive

        def join(self, *args, **kwargs):
            self.join_calls.append((args, kwargs))
            self._alive = False

    worker = SlowWorker()
    monkeypatch.setattr(plugins, "_background_discovery_thread", worker)
    monkeypatch.setattr(plugins, "in_plugin_load_worker", lambda: False)

    plugins._join_background_discovery()

    assert worker.join_calls == [((), {})]
