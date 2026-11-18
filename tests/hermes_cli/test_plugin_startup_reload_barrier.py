"""Strict gateway startup must wait for a concurrent control-socket plugin reload."""

import threading

import pytest

from tests.waiting import wait_until


@pytest.mark.parametrize("background_alive", [False, True], ids=["already-joined", "finishes-at-join"])
@pytest.mark.parametrize("reload_fails", [False, True], ids=["reload-completes", "reload-fails"])
def test_strict_startup_observes_reload_verdict_under_sweep_lock(monkeypatch, background_alive, reload_fails):
    from hermes_cli import plugins

    manager = plugins.PluginManager()
    manager._discovered = manager._discovery_complete = True
    entered, release, waiting, done = (threading.Event() for _ in range(4))
    outcomes = {}
    lock = manager._discovery_lock

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == "strict-startup":
                waiting.set()
            return lock.__enter__()

        def __exit__(self, *args):
            return lock.__exit__(*args)

    class BackgroundSweep:
        alive = background_alive

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.alive = False

    def load():
        entered.set()
        assert release.wait(10)
        if reload_fails:
            raise RuntimeError("reload failed")

    def reload():
        try:
            manager.discover_and_load(force=True)
        except Exception as exc:
            outcomes["reload_error"] = exc

    def startup():
        try:
            plugins.discover_plugins(require_complete=True)
            outcomes["startup_complete"] = manager._discovery_complete
        except Exception as exc:
            outcomes["startup_error"] = exc
        finally:
            done.set()

    monkeypatch.setattr(manager, "_discovery_lock", ObservedLock())
    monkeypatch.setattr(manager, "_discover_and_load_inner", load)
    monkeypatch.setattr(manager, "_refresh_secret_sources_after_discovery", lambda: None)
    monkeypatch.setattr(manager, "_re_register_config_hooks_after_force", lambda: None)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", BackgroundSweep())
    monkeypatch.setattr(plugins, "_background_discovery_error", None)
    worker = threading.Thread(target=reload, daemon=True)
    barrier = threading.Thread(target=startup, name="strict-startup", daemon=True)
    worker.start()
    try:
        assert entered.wait(5)
        assert not manager._discovery_complete
        barrier.start()
        assert wait_until(lambda: waiting.is_set() or done.is_set(), timeout=5)
        assert waiting.is_set(), outcomes
        assert not done.is_set(), "startup must not reject an in-flight reload as failed discovery"
        release.set()
        assert done.wait(5)
    finally:
        release.set()
        worker.join(5)
        if barrier.ident is not None:
            barrier.join(5)
    assert not worker.is_alive() and not barrier.is_alive()
    if reload_fails:
        assert isinstance(outcomes["reload_error"], RuntimeError)
        assert isinstance(outcomes["startup_error"], plugins.PluginDiscoveryIncomplete)
        assert "startup_complete" not in outcomes
    else:
        assert outcomes == {"startup_complete": True}
