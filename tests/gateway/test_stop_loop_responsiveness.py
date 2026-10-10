"""Gateway /stop writes must not stall unrelated asyncio work (#450)."""
from __future__ import annotations

import asyncio
import threading

import pytest

from tests.gateway.test_stop_pauses_goal_and_holds_wakes import (
    _active_goal, _isolated as _isolated, _runner,
)

# Reuse the real SQLite/runner isolation, not an invented store response.
pytestmark = pytest.mark.usefixtures("_isolated")


@pytest.mark.asyncio
async def test_generation_claim_on_loop_does_not_wait_for_stop_goal_io(monkeypatch):
    from hermes_cli.goals import GoalManager, GoalState
    from gateway.platforms.event import MessageEvent

    runner, _, source, key, session_id = await _runner(monkeypatch)
    _active_goal(session_id)
    writing, release, claimed, pulse_after_claim = (threading.Event() for _ in range(4))
    original_json = GoalState.to_json
    loop = asyncio.get_running_loop()
    delays = []
    ready, done = asyncio.Event(), asyncio.Event()

    def suspended_json(state):
        if state.status == "paused" and state.paused_reason == "user-interrupted (/stop)":
            writing.set()
            assert release.wait(timeout=5), "goal I/O was not released"
        return original_json(state)

    async def heartbeat():
        ready.set()
        while not done.is_set():
            started = loop.time()
            await asyncio.sleep(0.05)
            if claimed.is_set():
                delays.append(loop.time() - started)
                pulse_after_claim.set()

    def release_after_pulse():
        # A native thread can break the old loop-thread deadlock and leave a measurable failure.
        claimed.wait(timeout=2)
        pulse_after_claim.wait(timeout=2)
        release.set()

    monkeypatch.setattr(GoalState, "to_json", suspended_json)
    pulse_task = asyncio.create_task(heartbeat())
    await asyncio.wait_for(ready.wait(), timeout=2)
    stop_task = asyncio.create_task(runner._handle_stop_command(MessageEvent(text="/stop", source=source)))
    releaser = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(writing.wait, 2), timeout=3)
        releaser = threading.Thread(target=release_after_pulse)
        releaser.start()
        # This is the synchronous claim used by real inbound admission, on the asyncio thread.
        generation = runner._begin_session_run_generation(key)
        claimed.set()
        assert generation == 1
        await asyncio.wait_for(stop_task, timeout=5)
    finally:
        release.set()
        done.set()
        await asyncio.wait_for(pulse_task, timeout=2)
        await asyncio.wait_for(stop_task, timeout=2)
        if releaser is not None:
            await asyncio.to_thread(releaser.join, 2)
            assert not releaser.is_alive()
    assert delays and max(delays) < 0.2, f"50 ms heartbeat delayed to {max(delays):.3f}s"
    state = GoalManager(session_id).state
    assert state is not None and state.status == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "clear"])
async def test_stop_latch_store_io_is_off_loop(monkeypatch, action):
    import inspect
    from gateway.platforms.event import MessageEvent

    runner, _, source, key, _ = await _runner(monkeypatch)
    event = MessageEvent(text="/stop", source=source)
    if action == "clear":
        await runner._handle_stop_command(event)
    writing, release, pulse_after_write = (threading.Event() for _ in range(3))
    original_save = runner.session_store._save
    loop_thread = threading.get_ident()
    io_threads, delays = [], []
    ready, done = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()

    def suspended_save():
        if runner.session_store._entries[key].stop_latched == (action == "stop"):
            io_threads.append(threading.get_ident())
            writing.set()
            assert release.wait(timeout=5)
        return original_save()

    async def heartbeat():
        ready.set()
        while not done.is_set():
            start = loop.time()
            await asyncio.sleep(0.05)
            delays.append(loop.time() - start)
            if writing.is_set():
                pulse_after_write.set()

    def release_after_pulse():
        assert writing.wait(timeout=2)
        pulse_after_write.wait(timeout=2)  # breaks baseline's synchronous store write
        release.set()

    async def invoke():
        if action == "stop":
            return await runner._handle_stop_command(event)
        # Exercise the old synchronous and new asynchronous admission boundary identically.
        result = runner._clear_user_stop_latch(key)
        if inspect.isawaitable(result):
            await result

    monkeypatch.setattr(runner.session_store, "_save", suspended_save)
    pulse_task = asyncio.create_task(heartbeat())
    await asyncio.wait_for(ready.wait(), timeout=2)
    releaser = threading.Thread(target=release_after_pulse)
    releaser.start()
    try:
        await asyncio.wait_for(invoke(), timeout=5)
    finally:
        release.set()
        done.set()
        await asyncio.wait_for(pulse_task, timeout=2)
        await asyncio.to_thread(releaser.join, 2)
        assert not releaser.is_alive()
    assert writing.is_set() and io_threads
    assert loop_thread not in io_threads, "durable stop-latch I/O ran on the asyncio thread"
    assert delays and max(delays) < 0.2, f"50 ms heartbeat delayed to {max(delays):.3f}s"
    assert runner._user_stop_latched(key) == (action == "stop")


@pytest.mark.asyncio
async def test_delayed_stop_latch_write_cannot_rehold_after_user_admission(monkeypatch):
    runner, _, _, key, session_id = await _runner(monkeypatch)
    writing, release = threading.Event(), threading.Event()
    original_set = runner.session_store.set_stop_latched

    def delayed_set(key, latched, **kwargs):
        if latched:
            writing.set()
            assert release.wait(timeout=5)
        return original_set(key, latched, **kwargs)

    monkeypatch.setattr(runner.session_store, "set_stop_latched", delayed_set)
    revision = runner._latch_user_stop(key, session_id=session_id)
    stop_write = asyncio.create_task(runner._persist_user_stop_latch(
        key, session_id=session_id, revision=revision))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(writing.wait, 2), timeout=3)
        await asyncio.wait_for(runner._clear_user_stop_latch(key), timeout=2)
    finally:
        release.set()
        await asyncio.wait_for(stop_write, timeout=2)
    assert not runner._user_stop_latched(key)
    from gateway.session import SessionStore
    fresh = SessionStore(runner.session_store.sessions_dir, runner.config)
    assert not fresh.is_stop_latched(key, session_id=session_id)


@pytest.mark.asyncio
async def test_stop_goal_cas_preserves_goal_replaced_after_snapshot(monkeypatch):
    from gateway.platforms.event import MessageEvent
    from hermes_cli.goals import GoalManager
    import hermes_cli.goals_pause as goal_pause

    runner, _, source, key, session_id = await _runner(monkeypatch)
    _active_goal(session_id)
    writing, release = threading.Event(), threading.Event()
    original_pause = goal_pause.pause_goal_if_current

    def delayed_pause(*args, **kwargs):
        writing.set()
        assert release.wait(timeout=5)
        return original_pause(*args, **kwargs)

    monkeypatch.setattr(goal_pause, "pause_goal_if_current", delayed_pause)
    stop_task = asyncio.create_task(runner._handle_stop_command(MessageEvent(text="/stop", source=source)))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(writing.wait, 2), timeout=3)
        # A same-generation goal edit must not be lost either; the snapshot CAS is independent
        # of the route/generation fence, and SQLite has not acquired the pause's writer yet.
        await asyncio.wait_for(asyncio.to_thread(
            lambda: GoalManager(session_id).set("replacement after stop loaded its snapshot")), timeout=2)
    finally:
        release.set()
        await asyncio.wait_for(stop_task, timeout=2)
    state = GoalManager(session_id).state
    assert state is not None and state.status == "active"
    assert state.goal == "replacement after stop loaded its snapshot"
    assert runner._session_state(key).persistent.run_generation == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "clear"])
async def test_delayed_latch_write_follows_compression_but_not_reset(monkeypatch, action):
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionStore
    from hermes_cli.goals import GoalManager, _get_session_db, _meta_key
    from hermes_state import AsyncSessionDB

    runner, _, source, key, session_id = await _runner(monkeypatch)
    db = _get_session_db()
    assert db is not None
    runner._session_db = AsyncSessionDB(db)
    _active_goal(session_id)
    event = MessageEvent(text="/stop", source=source)
    if action == "clear":
        await runner._handle_stop_command(event)
    writing, release = threading.Event(), threading.Event()
    original_set = runner.session_store.set_stop_latched

    def delayed_set(key, latched, **kwargs):
        if latched == (action == "stop"):
            writing.set()
            assert release.wait(timeout=5)
        return original_set(key, latched, **kwargs)

    monkeypatch.setattr(runner.session_store, "set_stop_latched", delayed_set)
    owner = runner._stop_owner_session_entry(key)
    operation = (runner._handle_stop_command(event) if action == "stop"
                 else runner._clear_user_stop_latch(key))
    task = asyncio.create_task(operation)
    tip = session_id + "_compressed_during_stop"
    try:
        assert await asyncio.wait_for(asyncio.to_thread(writing.wait, 2), timeout=3)
        db.end_session(session_id, "compression")
        db.create_session(tip, source="telegram", parent_session_id=session_id,
                          model="test", model_config={})
        db.set_meta(_meta_key(tip), db.get_meta(_meta_key(session_id)))
        advanced = await runner.async_session_store.advance_compression_session(key, session_id, tip)
        assert advanced is owner and owner.session_id == tip
        if action == "stop":
            assert runner._user_stop_latched(key, session_ids=(tip,))
            assert await runner._completion_held_by_stop({
                "session_key": key, "parent_session_id": session_id})
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=2)
    fresh = SessionStore(runner.session_store.sessions_dir, runner.config)
    assert fresh.is_stop_latched(key, session_id=tip) == (action == "stop")
    assert runner._user_stop_latched(key, session_ids=(tip,)) == (action == "stop")
    assert await runner._completion_held_by_stop({
        "session_key": key, "parent_session_id": session_id}) == (action == "stop")
    if action == "stop":
        state = GoalManager(tip).state
        assert state is not None and state.status == "paused"
        replacement = await runner.async_session_store.reset_session(key)
        assert replacement is not owner
        assert not runner._user_stop_latched(key, session_ids=(replacement.session_id,))


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch", ["normal", "pending"])
async def test_pending_stop_warms_cold_route_off_loop(monkeypatch, dispatch):
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionStore
    from gateway.run import _AGENT_PENDING_SENTINEL
    from hermes_cli.goals import GoalManager
    from unittest.mock import AsyncMock

    runner, _, source, key, session_id = await _runner(monkeypatch)
    runner._session_state(key).turn.agent = _AGENT_PENDING_SENTINEL
    _active_goal(session_id)
    runner.session_store = SessionStore(runner.session_store.sessions_dir, runner.config)
    assert runner._stop_owner_session_entry(key) is None
    if dispatch == "pending":
        monkeypatch.setattr(runner, "_hm_busy_slash_or_photo", AsyncMock(return_value=(False, None)))
    await runner._hm_handle_running_session_message(MessageEvent(text="/stop", source=source), source, key)
    assert runner._user_stop_latched(key, session_ids=(session_id,))
    fresh = SessionStore(runner.session_store.sessions_dir, runner.config)
    assert await asyncio.to_thread(fresh.is_stop_latched, key, session_id=session_id)
    state = GoalManager(session_id).state
    assert state is not None and state.status == "paused"


@pytest.mark.asyncio
async def test_user_admission_clears_all_aliases_before_awaiting_store(monkeypatch):
    from gateway.platforms.event import MessageEvent

    runner, _, source, key, _ = await _runner(monkeypatch)
    await runner._handle_stop_command(MessageEvent(text="/stop", source=source))
    alias = key + ":admission_alias"
    runner._latch_user_stop(alias)
    writing, release = threading.Event(), threading.Event()
    original_set = runner.session_store.set_stop_latched

    def delayed_clear(key, latched, **kwargs):
        if not latched:
            writing.set()
            assert release.wait(timeout=5)
        return original_set(key, latched, **kwargs)

    monkeypatch.setattr(runner.session_store, "set_stop_latched", delayed_clear)
    clear_task = asyncio.create_task(runner._clear_user_stop_latch(key, alias))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(writing.wait, 2), timeout=3)
        assert not runner._session_state(key).conversation.stop_latched
        assert not runner._session_state(alias).conversation.stop_latched
        runner._latch_user_stop(alias)  # a new /stop while the older admission's store write waits
    finally:
        release.set()
        await asyncio.wait_for(clear_task, timeout=2)
    assert runner._user_stop_latched(alias)
    assert runner.session_store.is_stop_latched(key), "stale grouped clear must not undo the newer stop"
