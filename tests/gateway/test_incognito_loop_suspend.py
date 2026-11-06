"""A /loop pauses while its session is private and resumes on the same schedule after off.

A private interval withholds every loop write, so a wakeup would re-inject the same tick on
every scan, and its turn would carry private content into the --until judge.
"""
import asyncio
from functools import partial
from types import SimpleNamespace

import pytest

from agent.incognito import register_session
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import loops
from tests.gateway import test_incognito_turn_e2e as e2e
from tests.gateway import test_loop_command as loop_command
from tests.gateway.test_loop_command import _make_runner

harness = e2e.harness
loop_env = loop_command.loop_env

SID, KEY = "sid-gateway-loop", "agent:main:discord:channel:chat-loop"


def loop_state(sid):
    state = loops.load_loop(sid)
    assert state is not None
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_scan", "due_scan", "claim", "transition"])
async def test_private_loop_wakeup_injects_nothing_and_resumes_after_off(loop_env, boundary):
    from gateway.incognito import transition_scope

    runner = _make_runner()
    route = {"platform": "discord", "chat_id": "chat-loop", "chat_type": "channel", "user_id": "u"}
    mgr = loops.LoopManager(session_id=SID)
    mgr.set("poll CI", interval_seconds=300, route=route)
    mgr.state.next_due_at = 0
    loops.save_loop(SID, mgr.state)
    injected = []

    class _Adapter:
        async def handle_message(self, event):
            injected.append(event)
            event._gateway_accepted = True

    runner._adapters_for_profile = lambda profile: {Platform.DISCORD: _Adapter()}
    runner._build_process_event_source = lambda evt: SessionSource(
        platform=Platform.DISCORD, chat_id=evt["chat_id"], chat_type=evt["chat_type"], user_id=evt["user_id"])
    runner._session_key_for_source = lambda source: KEY
    runner._running_agents = {}

    entered = []
    transition_task: asyncio.Task[None] | None = None
    transition_entered, transition_release = asyncio.Event(), asyncio.Event()

    async def transition():
        # The gate runs settlement in its own task. The transition must enter and exit its
        # context in a separate, single owner task, just like a real /incognito command.
        with transition_scope(KEY):
            entered.append(True)
            transition_entered.set()
            await transition_release.wait()

    def enter():
        if not entered:
            register_session(SID, True)
            register_session(KEY, True)
            entered.append(True)

    async def _inline(func, *args):
        nonlocal transition_task
        if boundary == "claim" and isinstance(func, partial) and func.func.__name__ == "fire_tick":
            enter()
        result = func(*args)
        if getattr(func, "__name__", "") == "_due_manager":
            if boundary == "due_scan":
                enter()
            elif boundary == "transition" and not entered:
                transition_task = asyncio.create_task(transition())
                await transition_entered.wait()
        return result

    runner._run_in_executor_with_context = _inline
    before = loop_state(SID)
    if boundary == "before_scan":
        enter()
    try:
        for _scan in range(3):
            await GatewayRunner._loop_wakeup_fire_one(runner, SID, loop_state(SID), 1e12, set())
    finally:
        transition_release.set()
        if transition_task is not None:
            await transition_task
        register_session(SID, False)
        register_session(KEY, False)
    paused = loop_state(SID)
    assert entered == [True], "the test did not cross the selected privacy boundary"
    assert injected == []
    assert (paused.ticks_fired, paused.next_due_at, paused.awaiting_response) == (
        before.ticks_fired, before.next_due_at, before.awaiting_response)

    await GatewayRunner._loop_wakeup_fire_one(runner, SID, loop_state(SID), 1e12, set())
    assert len(injected) == 1 and loop_state(SID).ticks_fired == before.ticks_fired + 1


@pytest.fixture
def loop_topic(harness, monkeypatch):
    """A synthetic topic with the real privacy switch, store and durable loop manager."""
    from hermes_cli import goals

    runner, store, entry, _config, event, _db = harness
    goals._DB_CACHE.clear()
    goals._get_session_db()  # warm the loop store outside the async bootstrap path
    adapter = SimpleNamespace(handled=[], _active_sessions={})
    runner._adapters_for_profile = lambda profile: {Platform.TELEGRAM: adapter}
    runner._build_process_event_source = lambda route: event("").source
    runner._queue_depth = lambda key, adapter=None: 0
    runner._running = True

    async def execute(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def route(*_args):
        return True

    async def accept(incoming):
        # Real inline slash dispatch takes this route lock itself. A wake must not own it.
        async with runner._session_model_override_lock(entry.session_key):
            adapter.handled.append(incoming)
            incoming._gateway_accepted = True

    runner._run_in_executor_with_context = execute
    adapter.handle_message = accept
    monkeypatch.setattr("gateway.wake.automatic_route_available", route)
    manager = loops.LoopManager(entry.session_id)
    manager.set("/status", interval_seconds=300, route={
        "platform": "telegram", "chat_id": "123", "chat_type": "dm", "thread_id": "9", "user_id": "4",
    })
    assert manager.state is not None
    manager.state.next_due_at = 0
    loops.save_loop(entry.session_id, manager.state)

    async def fire():
        await runner._loop_wakeup_fire_one(entry.session_id, loop_state(entry.session_id), 1e12, set())

    yield SimpleNamespace(runner=runner, store=store, entry=entry, event=event, adapter=adapter, fire=fire)
    goals._DB_CACHE.clear()


def hold_loop_boundary(topic, monkeypatch, boundary):
    """Stop at a yielding production boundary without timing-dependent polling."""
    runner, entry = topic.runner, topic.entry
    reached, release = asyncio.Event(), asyncio.Event()

    async def hold():
        reached.set()
        await release.wait()

    execute = runner._run_in_executor_with_context

    async def executor(fn, *args, **kwargs):
        name = fn.func.__name__ if isinstance(fn, partial) else fn.__name__
        if boundary == "migration":
            # Replace only the scheduler's lineage reads; the real /incognito store stays intact.
            if name == "lookup_by_session_key":
                return SimpleNamespace(session_id="synthetic-compressed-child")
            if name == "<lambda>":
                return "synthetic-compressed-child"
        if (boundary, name) in {
            ("ownership", "lookup_by_session_key"), ("due", "_due_manager"),
            ("claim", "fire_tick"), ("settlement", "complete_tick"), ("recovery", "<lambda>"),
        }:
            await hold()
        return await execute(fn, *args, **kwargs)

    monkeypatch.setattr(runner, "_run_in_executor_with_context", executor)
    def hold_route():
        async def route(*_args):
            await hold()
            return True
        monkeypatch.setattr("gateway.wake.automatic_route_available", route)
    def hold_marker():
        original = runner._stale_resume_pending_marker

        async def marker(*args):
            await hold()
            return await original(*args)
        monkeypatch.setattr(runner, "_stale_resume_pending_marker", marker)
    def hold_dispatch():
        accept = topic.adapter.handle_message

        async def dispatch(incoming):
            await accept(incoming)
            if boundary == "dispatch":
                await hold()
            else:
                # Native adapters may release capacity before caller-owned settlement. A claimed
                # turn or expired reservation must not end the privacy fence either.
                from gateway.run_background_admission import BACKGROUND_RESERVATION_ATTR
                runner._release_background_turn(getattr(incoming, BACKGROUND_RESERVATION_ATTR))
                gate = runner._get_replay_scheduler().gate
                gate._claimed_since = lambda *_: True
                gate.load()
        topic.adapter.handle_message = dispatch
    def prepare_recovery():
        assert loops.LoopManager(entry.session_id).fire_tick()
    def hold_migration():
        migrate = runner._migrate_compressed_loop

        async def migration(*args):
            await hold()
            await migrate(*args)
        monkeypatch.setattr(runner, "_migrate_compressed_loop", migration)
    handlers = {
        "route": hold_route, "marker": hold_marker, "dispatch": hold_dispatch,
        "settlement": hold_dispatch, "recovery": prepare_recovery, "migration": hold_migration,
    }
    handler = handlers.get(boundary)
    if handler is not None:
        handler()
    return reached, release


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [
    "route", "ownership", "marker", "due", "claim", "dispatch", "settlement", "recovery", "migration",
])
async def test_real_incognito_switch_refuses_until_loop_bookkeeping_finishes(loop_topic, monkeypatch, boundary):
    topic = loop_topic
    reached, release = hold_loop_boundary(topic, monkeypatch, boundary)
    wake = asyncio.create_task(topic.fire())
    try:
        await asyncio.wait_for(reached.wait(), 2)
        reply = await asyncio.wait_for(e2e.incognito(topic.runner, topic.event, "/incognito on"), 2)
        assert "not changed" in reply and not topic.entry.incognito
        assert topic.runner._background_admission_pending(topic.entry.session_key)
        # /status remains usable during admission; there is no model-route lock around dispatch.
        assert "OFF" in await asyncio.wait_for(e2e.incognito(topic.runner, topic.event, "/incognito status"), 2)
    finally:
        release.set()
        await asyncio.wait_for(wake, 2)
    assert not topic.runner._background_admission_pending(topic.entry.session_key)
    state = loop_state(topic.entry.session_id)
    if boundary == "migration":
        assert state.status == "cleared"
        assert loop_state("synthetic-compressed-child") is not None
    else:
        assert state.ticks_fired == 1 and not state.awaiting_response
        assert len(topic.adapter.handled) == (0 if boundary == "recovery" else 1)
    assert "ON" in await e2e.incognito(topic.runner, topic.event, "/incognito on")
    assert "OFF" in await e2e.incognito(topic.runner, topic.event, "/incognito off")
    assert "ON" in await e2e.incognito(topic.runner, topic.event, "/incognito on")
    assert "OFF" in await e2e.incognito(topic.runner, topic.event, "/incognito off")


@pytest.mark.asyncio
async def test_real_incognito_holds_the_exact_loop_schedule_until_off(loop_topic):
    topic = loop_topic
    before = loop_state(topic.entry.session_id).to_json()
    assert "ON" in await e2e.incognito(topic.runner, topic.event, "/incognito on")
    for _scan in range(3):
        await topic.fire()
    assert loop_state(topic.entry.session_id).to_json() == before
    assert topic.adapter.handled == []
    assert "OFF" in await e2e.incognito(topic.runner, topic.event, "/incognito off")
    assert loop_state(topic.entry.session_id).to_json() == before
    await topic.fire()
    assert len(topic.adapter.handled) == 1
    state = loop_state(topic.entry.session_id)
    assert state.ticks_fired == 1 and not state.awaiting_response


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["due", "claim", "dispatch", "settlement", "recovery", "migration"])
async def test_cancelled_loop_admission_settles_before_releasing_privacy(loop_topic, monkeypatch, boundary):
    topic = loop_topic
    reached, release = hold_loop_boundary(topic, monkeypatch, boundary)
    wake = asyncio.create_task(topic.fire())
    try:
        await asyncio.wait_for(reached.wait(), 2)
        wake.cancel()
        await asyncio.sleep(0)
        assert topic.runner._background_admission_pending(topic.entry.session_key)
        assert "not changed" in await e2e.incognito(topic.runner, topic.event, "/incognito on")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await wake
    finally:
        release.set()
        await asyncio.gather(wake, return_exceptions=True)
    gate = topic.runner._get_replay_scheduler().gate
    assert not gate.admission_pending(topic.entry.session_key)
    # Successful admission retains capacity until the existing turn-claim owner takes it.
    assert gate.load() == (0 if boundary in {"settlement", "recovery", "migration"} else 1)
    gate._claimed_since = lambda *_: True
    assert gate.load() == 0
    assert "ON" in await e2e.incognito(topic.runner, topic.event, "/incognito on")
    assert "OFF" in await e2e.incognito(topic.runner, topic.event, "/incognito off")


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["claim", "admission", "settlement", "recovery", "migration"])
async def test_cancelled_loop_keeps_privacy_fenced_until_physical_write_settles(loop_topic, monkeypatch, boundary):
    """Cancelling an asyncio waiter must not expose a still-running executor or tracked write."""
    import threading
    from gateway.session_runtime_workers import tracked_write

    topic = loop_topic
    reached, released, finished = asyncio.Event(), threading.Event(), threading.Event()
    event_loop = asyncio.get_running_loop()
    execute = topic.runner._run_in_executor_with_context
    physical = []

    def blocked_write(fn, *args, **kwargs):
        event_loop.call_soon_threadsafe(reached.set)
        try:
            assert released.wait(5), "test did not release the physical write"
            result = fn(*args, **kwargs)
            physical.append(True)
            return result
        finally:
            finished.set()

    if boundary == "recovery":
        assert loops.LoopManager(topic.entry.session_id).fire_tick()
    target = {"claim": "fire_tick", "settlement": "complete_tick", "recovery": "<lambda>",
              "migration": "migrate_loop_to_session"}.get(boundary)

    async def executor(fn, *args, **kwargs):
        name = fn.func.__name__ if isinstance(fn, partial) else fn.__name__
        if boundary == "migration":
            if name == "lookup_by_session_key":
                return SimpleNamespace(session_id="synthetic-compressed-child")
            if name == "<lambda>":
                return "synthetic-compressed-child"
        if name == target:
            return await asyncio.to_thread(blocked_write, fn, *args, **kwargs)
        return await execute(fn, *args, **kwargs)

    monkeypatch.setattr(topic.runner, "_run_in_executor_with_context", executor)
    if boundary == "admission":
        authority = SimpleNamespace()

        async def admit(incoming):
            await tracked_write(authority, lambda: blocked_write(lambda: None))
            incoming._gateway_accepted = True
        topic.adapter.handle_message = admit
    wake = asyncio.create_task(topic.fire())
    try:
        await asyncio.wait_for(reached.wait(), 2)
        wake.cancel()
        await asyncio.sleep(0)  # let cancellation reach the physical write's asyncio waiter
        reply = await asyncio.wait_for(e2e.incognito(topic.runner, topic.event, "/incognito on"), 2)
        assert "not changed" in reply and not topic.entry.incognito
        assert topic.runner._background_admission_pending(topic.entry.session_key)
        assert not wake.done(), "cancelled caller released its still-running physical write"
        wake.cancel()  # repeated cancellation must not separate the worker from its fence either
        await asyncio.sleep(0)
        assert topic.runner._background_admission_pending(topic.entry.session_key)
    finally:
        released.set()
        result = await asyncio.wait_for(asyncio.gather(wake, return_exceptions=True), 2)
        assert await asyncio.to_thread(finished.wait, 2)
    assert isinstance(result[0], asyncio.CancelledError)
    assert physical == [True]
    gate = topic.runner._get_replay_scheduler().gate
    assert not gate.admission_pending(topic.entry.session_key)
    assert gate.load() == (0 if boundary in {"recovery", "migration"} else 1)
    gate._claimed_since = lambda *_: True
    assert gate.load() == 0
    assert "ON" in await e2e.incognito(topic.runner, topic.event, "/incognito on")
    assert "OFF" in await e2e.incognito(topic.runner, topic.event, "/incognito off")


@pytest.mark.asyncio
@pytest.mark.parametrize("deferral", ["not_due", "route", "owner", "goal", "gate"])
async def test_unadmitted_loop_scan_returns_its_background_capacity(loop_topic, monkeypatch, deferral):
    topic = loop_topic
    def not_due():
        state = loop_state(topic.entry.session_id)
        state.next_due_at = 2e12
        loops.save_loop(topic.entry.session_id, state)
    def unavailable_route():
        async def unavailable(*_args):
            return False
        monkeypatch.setattr("gateway.wake.automatic_route_available", unavailable)
    def busy_owner():
        topic.runner._running_agents[topic.entry.session_key] = object()
    def active_goal():
        state = loop_state(topic.entry.session_id)
        state.next_due_at = 1e12 - 1
        loops.save_loop(topic.entry.session_id, state)
        monkeypatch.setattr(loops, "goal_blocks_loop_tick", lambda sid: True)
    def full_gate():
        monkeypatch.setattr(topic.runner, "_reserve_background_turn", lambda *_args: None)

    handlers = {"not_due": not_due, "route": unavailable_route, "owner": busy_owner,
                "goal": active_goal, "gate": full_gate}
    handlers[deferral]()
    before = loop_state(topic.entry.session_id).to_json()
    for _scan in range(3):
        await topic.fire()
    assert topic.runner._get_replay_scheduler().gate.load() == 0
    assert not topic.runner._background_admission_pending(topic.entry.session_key)
    assert loop_state(topic.entry.session_id).to_json() == before
    assert topic.adapter.handled == []


@pytest.mark.asyncio
async def test_real_adapter_acceptance_must_fence_the_pre_agent_handoff(loop_topic, monkeypatch):
    """An accepted wake owns its adapter guard before publishing the model-turn slot."""
    from evals.heartbeat_idle_wire import WireAdapter
    from gateway.config import PlatformConfig

    topic = loop_topic
    runner, key = topic.runner, topic.entry.session_key
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._adapters_for_profile = lambda profile: runner.adapters
    runner._queue_depth = GatewayRunner._queue_depth.__get__(runner)
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(event):
        return None

    async def pending_slot(event, session_key):
        entered.set()
        await release.wait()

    adapter._message_handler = handler
    monkeypatch.setattr(adapter, "_process_message_background", pending_slot)
    state = loop_state(topic.entry.session_id)
    state.prompt = "Check the public report"
    loops.save_loop(topic.entry.session_id, state)
    try:
        await topic.fire()
        await asyncio.wait_for(entered.wait(), 2)
        state = loop_state(topic.entry.session_id)
        assert state.awaiting_response and state.ticks_fired == 1
        assert key in adapter._active_sessions and runner._running_agents.get(key) is None
        assert runner._get_replay_scheduler().gate.load() == 1
        reply = await asyncio.wait_for(e2e.incognito(runner, topic.event, "/incognito on"), 2)
        assert "not changed" in reply, "accepted wake has no agent slot yet: " + reply
    finally:
        release.set()
        await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)
        adapter._active_sessions.clear()


@pytest.mark.asyncio
async def test_cross_session_continuation_for_a_private_target_is_held():
    runner = object.__new__(GatewayRunner)
    record = {"id": "control-1", "target_session_id": "sid-private-target"}
    admitted = []

    async def admit(*_args):
        admitted.append(True)

    runner._mark_and_finish = admit
    register_session("sid-private-target", True)
    try:
        held = await runner._admit_control_continuation(record, None, None, None, "key-private", "continue")
    finally:
        register_session("sid-private-target", False)
    assert held is record and admitted == []
