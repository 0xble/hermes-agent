"""Ingress policy must see a complete registry without stalling the gateway loop."""

import asyncio
import threading
from unittest.mock import Mock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run_inbound import GatewayInboundMixin
from gateway.run_startup import GatewayStartupMixin
from gateway.session import SessionSource


@pytest.mark.asyncio
@pytest.mark.parametrize("reload", [False, True], ids=["cold-start", "forced-reload"])
@pytest.mark.parametrize("action", ["skip", "rewrite"])
async def test_first_ingress_enforces_policy_after_discovery(monkeypatch, reload, action):
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    if reload:
        manager._discovered = manager._discovery_complete = True
    entered, release = threading.Event(), threading.Event()
    barrier = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    seen = []
    lock = manager._discovery_lock

    class ObservedLock:
        def acquire(self, **kwargs):
            if kwargs.get("blocking") is False:
                return lock.acquire(blocking=False)
            assert threading.get_ident() != loop_thread, "discovery blocked the loop"
            loop.call_soon_threadsafe(barrier.set)
            return lock.acquire(**kwargs)

        def release(self):
            lock.release()

    def policy(**kwargs):
        seen.append(kwargs["event"].text)
        return {"action": action, "text": "redacted"}

    def load():
        assert threading.get_ident() != loop_thread, "cold discovery ran on the loop"
        entered.set()
        loop.call_soon_threadsafe(barrier.set)
        assert release.wait(15)
        manager._hooks["pre_gateway_dispatch"] = [policy]

    monkeypatch.setattr(manager, "_discover_and_load_inner", load)
    monkeypatch.setattr(manager, "_refresh_secret_sources_after_discovery", lambda: None)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", None)
    worker = None
    if reload:
        worker = threading.Thread(target=manager.discover_and_load, kwargs={"force": True})
        worker.start()
        assert await asyncio.to_thread(entered.wait, 5)
        barrier.clear()
        monkeypatch.setattr(manager, "_discovery_lock", ObservedLock())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="test", user_id="test", chat_type="dm")
    event = MessageEvent(text="sensitive", source=source)
    runner = GatewayInboundMixin()
    delivered = asyncio.create_task(runner._hm_pre_gateway_dispatch_hook(event, source))
    try:
        await asyncio.wait_for(barrier.wait(), 5)
        assert not delivered.done(), "ingress passed an incomplete policy registry"
        assert seen == []
        release.set()
        result = await delivered
        assert seen == ["sensitive"]
        if action == "skip":
            assert result is None
        else:
            assert result is event
            assert result.text == "redacted"
    finally:
        release.set()
        await delivered
        if worker is not None:
            await asyncio.to_thread(worker.join, 5)
            assert not worker.is_alive()


@pytest.mark.asyncio
async def test_reload_starting_after_ready_probe_cannot_expose_empty_policy(monkeypatch):
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    manager._discovered = manager._discovery_complete = True
    loop_thread = threading.get_ident()
    attempts = []

    class RacingLock:
        def acquire(self, *, blocking=True, timeout=-1):
            attempts.append((threading.get_ident(), blocking))
            if not blocking and len(attempts) == 1:
                # A force sweep wins the lock after the ready probe, before snapshot.
                manager._discovery_complete = False
                manager._hooks.clear()
                return False
            if blocking:
                assert threading.get_ident() != loop_thread
                manager._hooks["pre_gateway_dispatch"] = [lambda **_: {"action": "skip"}]
                manager._discovery_complete = True
            return True

        def release(self):
            pass

    monkeypatch.setattr(manager, "_discovery_lock", RacingLock())
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", None)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="test", user_id="test", chat_type="dm")
    event = MessageEvent(text="sensitive", source=source)
    assert await GatewayInboundMixin()._hm_pre_gateway_dispatch_hook(event, source) is None
    assert [blocking for _, blocking in attempts] == [False, True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "load-error"])
async def test_incomplete_discovery_does_not_authorize_ingress(monkeypatch, failure):
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()

    class SlowThread:
        def is_alive(self):
            return True

        def join(self, timeout):
            pass

    def broken_load():
        raise ValueError("broken discovery")

    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", SlowThread() if failure == "timeout" else None)
    monkeypatch.setattr(manager, "_discover_and_load_inner", broken_load)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="test", user_id="test", chat_type="dm")
    event = MessageEvent(text="sensitive", source=source)
    with pytest.raises(plugins.PluginDiscoveryIncomplete, match="discovery incomplete"):
        await GatewayInboundMixin()._hm_pre_gateway_dispatch_hook(event, source)


def test_startup_refuses_worker_that_exits_incomplete(monkeypatch):
    import hermes_cli.plugins as plugins
    import gateway.relay as relay

    class FailedThread:
        alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.alive = False

    manager = plugins.PluginManager()
    manager._discovered = True
    relay_registration = Mock()
    config_hooks = Mock()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", FailedThread())
    monkeypatch.setattr("hermes_cli.plugins_cmd_remove.sweep_pending_plugin_deletes", lambda: None)
    monkeypatch.setattr(relay, "self_provision_relay", lambda: None)
    monkeypatch.setattr(relay, "register_relay_adapter", relay_registration)
    monkeypatch.setattr(GatewayStartupMixin, "_register_launch_profile_config_hooks", config_hooks)

    with pytest.raises(RuntimeError, match="discovery"):
        GatewayStartupMixin._start_register_plugins_relay_hooks()
    relay_registration.assert_not_called()
    config_hooks.assert_not_called()


@pytest.mark.asyncio
async def test_ready_registry_waits_off_loop_for_contended_lock_without_busy_spin(monkeypatch):
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    manager._discovered = manager._discovery_complete = True
    lock = manager._discovery_lock
    entered, release = threading.Event(), threading.Event()
    barrier = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    attempts = []
    thread_hops = []
    to_thread = asyncio.to_thread

    class ObservedLock:
        def acquire(self, *, blocking=True, timeout=-1):
            attempts.append((threading.get_ident(), blocking, timeout))
            if not blocking:
                assert len(attempts) <= 3, "ready-lock contention caused a busy-spin"
                return lock.acquire(blocking=False)
            assert threading.get_ident() != loop_thread, "lock wait blocked the loop"
            loop.call_soon_threadsafe(barrier.set)
            return lock.acquire(timeout=timeout)

        def release(self):
            lock.release()

    async def counted_to_thread(func, *args, **kwargs):
        thread_hops.append(func)
        assert len(thread_hops) == 1, "ready-lock contention repeated worker hops without waiting"
        return await to_thread(func, *args, **kwargs)

    def hold_ready_registry():
        # Deferred loading, activation and unload can hold the lock with readiness intact.
        with lock:
            entered.set()
            assert release.wait(15)
            manager._hooks["pre_gateway_dispatch"] = [lambda **_: {"action": "skip"}]

    monkeypatch.setattr(manager, "_discovery_lock", ObservedLock())
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", None)
    monkeypatch.setattr(asyncio, "to_thread", counted_to_thread)
    worker = threading.Thread(target=hold_ready_registry, daemon=True)
    worker.start()
    assert entered.wait(5)
    delivered = asyncio.create_task(plugins.ainvoke_hook("pre_gateway_dispatch"))
    reached = asyncio.create_task(barrier.wait())
    try:
        done, _ = await asyncio.wait({delivered, reached}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        if delivered in done:
            await delivered  # Surface a repeated-hop failure rather than a guard timeout.
        assert reached in done, "ready registry never waited for contention off-loop"
        assert not delivered.done()
        assert len(thread_hops) == 1
        release.set()
        assert await delivered == [{"action": "skip"}]
        assert [blocking for _, blocking, _ in attempts] == [False, True, False]
        assert attempts[1][2] > 0, "worker lock wait needs a bounded timeout"
    finally:
        release.set()
        reached.cancel()
        await asyncio.gather(reached, delivered, return_exceptions=True)
        worker.join(5)
    assert not worker.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "lock-error"])
async def test_ready_registry_contention_timeout_refuses_policy_without_repeated_hops(monkeypatch, failure):
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    manager._discovered = manager._discovery_complete = True
    loop_thread = threading.get_ident()
    attempts = []

    class TimedOutLock:
        def acquire(self, *, blocking=True, timeout=-1):
            attempts.append((threading.get_ident(), blocking, timeout))
            assert len(attempts) <= 2, "ready-lock timeout caused repeated acquire attempts"
            if blocking:
                assert threading.get_ident() != loop_thread
                assert timeout > 0
                if failure == "lock-error":
                    raise OSError("registry lock wait failed")
            return False

        def release(self):
            pytest.fail("must not release an unacquired lock")

    monkeypatch.setattr(manager, "_discovery_lock", TimedOutLock())
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", None)
    with pytest.raises(plugins.PluginDiscoveryIncomplete, match="discovery incomplete"):
        await plugins.ainvoke_hook("pre_gateway_dispatch")
    assert [blocking for _, blocking, _ in attempts] == [False, True]
