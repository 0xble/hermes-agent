"""Regression tests for plugin delivery during asynchronous startup."""

from __future__ import annotations

from contextlib import contextmanager

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


@contextmanager
def _inflight_discovery(monkeypatch):
    """Register late only when a delivery joins the in-flight discovery worker."""
    import threading

    import hermes_cli.plugins as plugins
    from tests.plugin_helpers import mark_plugin_discovery_started

    manager = plugins.PluginManager()
    mark_plugin_discovery_started(manager)

    def callback(**_kwargs):
        return None

    started, release = threading.Event(), threading.Event()

    def late_registration():
        started.set()
        assert release.wait(10)
        manager._hooks.setdefault("pre_llm_call", []).append(callback)
        manager._discovery_complete = True

    join = plugins._join_background_discovery

    def join_and_release():
        release.set()
        join()

    worker = threading.Thread(target=late_registration, name="plugin-discovery", daemon=True)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", worker)
    monkeypatch.setattr(plugins, "_join_background_discovery", join_and_release)
    worker.start()
    try:
        assert started.wait(5)
        yield plugins, callback
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


def test_has_hook_sees_hook_registered_by_inflight_discovery(monkeypatch):
    with _inflight_discovery(monkeypatch) as (plugins, _callback):
        assert plugins.has_hook("pre_llm_call") is True


def test_streaming_hook_snapshot_sees_hook_registered_by_inflight_discovery(monkeypatch):
    with _inflight_discovery(monkeypatch) as (plugins, callback):
        assert callback in plugins.iter_hook_callbacks("pre_llm_call")


@pytest.mark.parametrize("same_tool_call", [False, True], ids=["independent-calls", "same-call-reentry"])
def test_concurrent_first_delivery_waits_for_sibling_foreground_discovery(monkeypatch, same_tool_call):
    """Independent deliveries wait for discovery, without colliding in the callback gate."""
    import threading

    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    entered, release = threading.Event(), threading.Event()
    sibling_waiting, first_callback = threading.Event(), threading.Event()
    release_callback, sibling_done = threading.Event(), threading.Event()
    seen, result, first_result = [], {}, {}

    def callback(**kwargs):
        seen.append(kwargs["n"])
        if kwargs["n"] == 1:
            first_callback.set()
            assert release_callback.wait(10)
        return "ran"

    def load():
        entered.set()
        assert release.wait(10)
        manager._hooks.setdefault("post_tool_call", []).append(callback)

    await_discovery = plugins._await_foreground_discovery

    def observe_wait(active_manager):
        if threading.current_thread().name == "sibling-delivery":
            sibling_waiting.set()
        return await_discovery(active_manager)

    invoke = manager.invoke_hook

    def ordered_delivery(hook_name, **kwargs):
        # Hold the first callback open while the sibling dispatches. A fixture accidentally
        # giving both calls the same identity must fail deterministically, not pass by luck.
        if kwargs["n"] == 2:
            assert first_callback.wait(5)
        return invoke(hook_name, **kwargs)

    def deliver(n, destination):
        try:
            call_id = "first-tool" if n == 1 or same_tool_call else "sibling-tool"
            destination["value"] = plugins.invoke_hook("post_tool_call", n=n, tool_call_id=call_id)
        except Exception as exc:  # surfaced in the main test thread
            destination["error"] = exc
        finally:
            if n == 2:
                sibling_done.set()

    monkeypatch.setattr(manager, "_discover_and_load_inner", load)
    monkeypatch.setattr(manager, "_refresh_secret_sources_after_discovery", lambda: None)
    monkeypatch.setattr(plugins, "_await_foreground_discovery", observe_wait)
    monkeypatch.setattr(manager, "invoke_hook", ordered_delivery)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    first = threading.Thread(target=deliver, args=(1, first_result), daemon=True)
    worker = threading.Thread(target=deliver, args=(2, result), name="sibling-delivery", daemon=True)
    first.start()
    try:
        assert entered.wait(5)
        worker.start()
        assert sibling_waiting.wait(5), "sibling must reach the discovery barrier"
        assert result == {}
        assert seen == [], "no callback may see the partial registry"
        release.set()
        assert first_callback.wait(5)
        assert sibling_done.wait(5)
        assert result == {"value": [] if same_tool_call else ["ran"]}
        assert sorted(seen) == ([1] if same_tool_call else [1, 2])
    finally:
        release.set()
        release_callback.set()
        first.join(5)
        if worker.ident is not None:
            worker.join(5)
    assert not first.is_alive()
    assert not worker.is_alive()
    assert first_result == {"value": ["ran"]}


@pytest.mark.asyncio
async def test_ready_async_delivery_preserves_inline_ingress_order(monkeypatch):
    import asyncio

    import hermes_cli.plugins as plugins
    from tests.plugin_helpers import mark_plugin_discovery_complete

    manager = plugins.PluginManager()
    mark_plugin_discovery_complete(manager)
    seen = []
    manager._hooks["pre_gateway_dispatch"] = [lambda **_: seen.append("policy")]
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", None)

    async def unexpected_thread_hop(*_args, **_kwargs):
        pytest.fail("a ready registry must not yield before session fencing")

    monkeypatch.setattr(asyncio, "to_thread", unexpected_thread_hop)
    asyncio.get_running_loop().call_soon(seen.append, "next-loop-turn")

    await plugins.ainvoke_hook("pre_gateway_dispatch")
    assert seen == ["policy"], "a ready registry must not yield before session fencing"


def test_async_delivery_does_not_block_the_event_loop_on_discovery_lock(monkeypatch):
    import asyncio
    import threading

    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    seen = []
    loop_thread = threading.get_ident()
    lock = manager._discovery_lock

    class ObservedLock:
        def acquire(self, **kwargs):
            if kwargs.get("blocking") is False:
                return lock.acquire(blocking=False)
            assert threading.get_ident() != loop_thread, "discovery lock blocked the event-loop thread"
            waiting.set()
            return lock.acquire(**kwargs)

        def release(self):
            lock.release()

    def reload():
        with lock:
            manager._discovered = True
            manager._discovery_complete = False
            entered.set()
            assert release.wait(15)
            manager._hooks["pre_gateway_dispatch"] = [lambda **kwargs: seen.append("ready") or "ready"]
            manager._discovery_complete = True

    monkeypatch.setattr(manager, "_discovery_lock", ObservedLock())
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_background_discovery_thread", None)
    worker = threading.Thread(target=reload)
    worker.start()
    assert entered.wait(5)

    async def exercise():
        delivered = asyncio.create_task(plugins.ainvoke_hook("pre_gateway_dispatch"))
        try:
            reached = await asyncio.to_thread(waiting.wait, 5)
            if not reached:
                release.set()
                await delivered  # surface the wrong-thread lock assertion, not a watchdog symptom
            assert reached, "delivery never reached the barrier off-loop"
            assert not delivered.done()
            assert seen == [], "no delivery against the half-loaded registry"
        finally:
            release.set()
        assert await delivered == ["ready"]

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert seen == ["ready"]


def test_sync_event_loop_observer_probe_snapshots_but_policy_refuses_unready_discovery(monkeypatch):
    import asyncio
    import hermes_cli.plugins as plugins

    manager = plugins.PluginManager()
    manager._discovered = True
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_join_background_discovery", lambda: pytest.fail("joined on loop"))
    monkeypatch.setattr(plugins, "_await_foreground_discovery", lambda _: pytest.fail("locked on loop"))

    async def exercise():
        assert not plugins.has_hook("gateway_platform_event")
        with pytest.raises(RuntimeError, match="discovery incomplete"):
            plugins.has_hook("pre_gateway_dispatch")
        directive = plugins._get_pre_tool_call_directive_details("terminal", {})
        assert directive.action == "block"
        assert "unavailable" in (directive.message or "")

    asyncio.run(exercise())


@pytest.mark.parametrize("listing", ["discover", "skill_commands"])
def test_discovery_listing_degrades_when_background_join_times_out(monkeypatch, caplog, listing):
    import hermes_cli.plugins as plugins

    class SlowThread:
        def is_alive(self):
            return True

        def join(self, timeout):
            return None

    class Manager:
        def discover_and_load(self, *, force=False):
            pytest.fail("must not race a second discovery after timeout")

        def list_plugin_skill_metadata(self):
            return []

    monkeypatch.setattr(plugins, "_background_discovery_thread", SlowThread())
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: Manager())
    with caplog.at_level("WARNING"):
        if listing == "discover":
            plugins.discover_plugins()
        else:
            from agent.skill_commands import get_plugin_skill_commands
            assert get_plugin_skill_commands() == {}
    assert "background plugin discovery did not finish" in caplog.text


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
