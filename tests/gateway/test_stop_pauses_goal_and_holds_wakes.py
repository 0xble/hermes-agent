"""Invariant: a gateway ``/stop`` stays stopped until the user sends something.

Before this, ``/stop`` killed the turn and its background delegations, but (1) the standing
``/goal`` stayed active and (2) the delegations' own "interrupted" completions were injected as a
fresh turn one second later. That turn's post-turn judge queued goal continuations, so the agent
kept talking after the user stopped it. The CLI already paused the goal on Ctrl+C.

Real ``GatewayRunner`` + real ``BasePlatformAdapter`` subclass, real goal SQLite state, real
``tools.async_delegation`` durable rows; no Telegram or LLM traffic.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

import tools.async_delegation as ad
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="x"), Platform.TELEGRAM)
        self.sent: list[str] = []

    @property
    def name(self):
        return "telegram"

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "private"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    goals._get_session_db()  # warm off the loop thread (see test_goal_verdict_send)
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()
    goals._DB_CACHE.clear()


async def _runner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "u1")
    adapter = _Adapter()
    runner = GatewayRunner(config=GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="x")}))
    runner.adapters = {Platform.TELEGRAM: adapter}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm", user_id="u1", user_name="tester")
    key = adapter._event_session_key(MessageEvent(text="", message_type=MessageType.TEXT, source=source))
    # Route through the real store so the in-memory index and state.db contain the same
    # session.  A hand-inserted SessionEntry leaves the durable row absent, and the
    # production stale-route guard then self-heals it to a different session during /stop.
    entry = await runner.async_session_store.get_or_create_session(source)
    session_id = entry.session_id
    return runner, adapter, source, key, session_id


def _active_goal(session_id: str):
    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_id)
    mgr.set("migrate the persistent agent stack")
    return mgr


def _blocked_goal(session_id: str):
    mgr = _active_goal(session_id)
    with patch("hermes_cli.goals.judge_goal",
               return_value=("blocked", "needs the user to answer", False, None, False)):
        assert mgr.evaluate_after_turn("May I proceed?")["status"] == "paused"
    return mgr


def _interrupted_completion(key: str, delegation_id: str, *, parent_session_id: str = "") -> dict:
    evt = {"type": "async_delegation", "session_key": key, "delegation_id": delegation_id,
           "summary": None, "error": "interrupted", "status": "interrupted", "dispatched_at": time.time(),
           "platform": "telegram", "chat_type": "dm", "chat_id": "c1", "user_id": "u1",
           "parent_session_id": parent_session_id}
    ad._persist_dispatch(evt)
    ad._persist_completion(evt, {"status": "interrupted", "summary": None})
    return evt


@pytest.mark.asyncio
@pytest.mark.parametrize("session_state", ["idle", "busy"])
@pytest.mark.parametrize("goal_state", ["active", "judge-blocked"])
async def test_stop_pauses_goal_so_user_input_does_not_revive_it(monkeypatch, session_state, goal_state):
    from hermes_cli.goals import GoalManager

    runner, adapter, source, key, session_id = await _runner(monkeypatch)
    (_active_goal if goal_state == "active" else _blocked_goal)(session_id)
    continuation = runner._synthetic_prompt_event(source, "[Continuing toward your standing goal]\nGoal: x")
    adapter._pending_messages[key] = continuation

    if session_state == "busy":
        await runner._interrupt_and_clear_session(key, source, interrupt_reason="Stop requested",
                                                  invalidation_reason="stop_command")
    else:
        reply = await runner._handle_stop_command(MessageEvent(text="/stop", message_type=MessageType.TEXT,
                                                               source=source))
        assert "Stopped" in str(reply)

    state = GoalManager(session_id).state
    assert state.status == "paused"
    assert state.paused_reason == "user-interrupted (/stop)"
    assert key not in adapter._pending_messages  # queued continuation dropped
    assert any(m.startswith("⏸ Goal paused") for m in adapter.sent)
    # An explicit stop is not a judge BLOCK: ordinary user input must not revive it.
    assert GoalManager(session_id).resume_for_user_input() is False


@pytest.mark.asyncio
async def test_completions_from_a_stop_are_held_until_the_user_sends_a_turn(monkeypatch):
    runner, adapter, source, key, _session_id = await _runner(monkeypatch)
    received: list[MessageEvent] = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)
    await runner._interrupt_and_clear_session(key, source, interrupt_reason="Stop requested",
                                              invalidation_reason="stop_command")
    group = [_interrupted_completion(key, f"deleg_stop_{i}") for i in range(2)]

    for _ in range(3):  # the watcher retries every tick; holding must never spend an attempt
        assert await runner._deliver_async_delegation_group(group) is False
    assert not received
    for evt in group:
        row = ad.get_durable_delegation(evt["delegation_id"])
        assert (row["delivery_state"], row["delivery_attempts"]) == ("pending", 0)

    # The user's next admitted turn releases the hold; internal wakes never do.
    assert runner._is_user_turn_event(MessageEvent(text="internal", source=source, internal=True)) is False
    assert runner._is_user_turn_event(MessageEvent(text="continue", source=source)) is True
    await runner._clear_user_stop_latch(key)
    assert await runner._deliver_async_delegation_group(group) is True
    for task in list(adapter._background_tasks):
        await task
    assert len(received) == 1 and received[0].internal


@pytest.mark.asyncio
async def test_stop_latch_survives_gateway_restart(monkeypatch):
    runner, _adapter, source, key, _session_id = await _runner(monkeypatch)
    await runner._interrupt_and_clear_session(key, source, interrupt_reason="Stop requested",
                                              invalidation_reason="stop_command")
    assert runner._user_stop_latched(key)

    restarted = GatewayRunner(config=GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="x")}))
    await restarted.async_session_store.get_or_create_session(source)
    assert restarted._user_stop_latched(key)


@pytest.mark.asyncio
async def test_compression_ancestor_completion_stays_held_after_restart(monkeypatch):
    from hermes_cli.goals import _get_session_db

    runner, _adapter, source, key, parent_id = await _runner(monkeypatch)
    evt = _interrupted_completion(key, "deleg_compressed_stop", parent_session_id=parent_id)
    db = _get_session_db()
    assert db is not None
    tip_id = "compressed_stop_tip"
    await asyncio.to_thread(db.end_session, parent_id, "compression")
    await asyncio.to_thread(db.create_session, tip_id, "telegram", parent_session_id=parent_id)
    assert await runner.async_session_store.advance_compression_session(key, parent_id, tip_id)
    await runner._handle_stop_command(MessageEvent(text="/stop", source=source))

    restarted, adapter, _, _, restored_id = await _runner(monkeypatch)
    assert restored_id == tip_id
    assert restarted._user_stop_latched(key, session_ids=(tip_id,))
    received = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)
    try:
        assert await restarted._deliver_async_delegation_group([evt]) is False
    finally:
        for task in list(adapter._background_tasks):
            await asyncio.wait_for(task, timeout=2)
    assert not received
    row = ad.get_durable_delegation(evt["delegation_id"])
    assert (row["delivery_state"], row["delivery_attempts"]) == ("pending", 0)

    await restarted._clear_user_stop_latch(key)
    assert await restarted._deliver_async_delegation_group([evt]) is True
    for task in list(adapter._background_tasks):
        await asyncio.wait_for(task, timeout=2)
    assert len(received) == 1 and received[0].internal

    # A compression chain ending at a user reset must not carry its ancestor's stop hold.
    replacement = await restarted.async_session_store.reset_session(key)
    assert replacement is not None and replacement.session_id != tip_id
    assert not restarted._user_stop_latched(key)
    assert not await restarted._completion_held_by_stop(evt)
    await restarted._handle_stop_command(MessageEvent(text="/stop", source=source))
    assert restarted._user_stop_latched(key, session_ids=(replacement.session_id,))
    assert not await restarted._completion_held_by_stop(evt)  # live must agree with restart
    after_reset, _, _, _, reset_id = await _runner(monkeypatch)
    assert reset_id == replacement.session_id
    assert after_reset._user_stop_latched(key, session_ids=(reset_id,))
    assert not await after_reset._completion_held_by_stop(evt)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["warmup", "executor"])
@pytest.mark.parametrize("owner_change", ["generation", "route"])
async def test_stop_pause_after_await_does_not_touch_newer_turn_goal(monkeypatch, boundary, owner_change):
    from hermes_cli.goals import GoalManager

    runner, adapter, source, key, session_id = await _runner(monkeypatch)
    _active_goal(session_id)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_warm = runner._warm_goals_session_db
    original_execute = runner._run_in_executor_with_context

    async def suspended_warm(label):
        if label == "goal stop" and boundary == "warmup":
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=2)
        return await original_warm(label)

    async def suspended_execute(job, *args, **kwargs):
        if job.__name__ == "_pause" and boundary == "executor":
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=2)
        return await original_execute(job, *args, **kwargs)

    monkeypatch.setattr(runner, "_warm_goals_session_db", suspended_warm)
    monkeypatch.setattr(runner, "_run_in_executor_with_context", suspended_execute)
    stop_task = asyncio.create_task(runner._handle_stop_command(MessageEvent(text="/stop", source=source)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if owner_change == "generation":
            runner._begin_session_run_generation(key)
            newer_session_id = session_id
        else:
            replacement = await runner.async_session_store.reset_session(key)
            newer_session_id = replacement.session_id
            # Isolate the route fence: do not advance the generation in this branch.
        await asyncio.to_thread(GoalManager(newer_session_id).set, "finish the newer user request")
        release.set()
        await asyncio.wait_for(stop_task, timeout=2)
    finally:
        release.set()
        if not stop_task.done():
            await asyncio.wait_for(stop_task, timeout=2)

    state = GoalManager(newer_session_id).state
    assert state is not None and state.goal == "finish the newer user request"
    assert state.status == "active"
    assert GoalManager(session_id).state.status == "active"
    assert not any(message.startswith("⏸ Goal paused") for message in adapter.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_kind", ["generation", "route"])
async def test_stop_pause_write_is_atomic_with_owner_claim(monkeypatch, claim_kind):
    import threading
    from hermes_cli.goals import GoalManager, GoalState

    runner, _adapter, source, key, session_id = await _runner(monkeypatch)
    _active_goal(session_id)
    writing = threading.Event()
    release = threading.Event()
    claiming = threading.Event()
    setting = threading.Event()
    original_json = GoalState.to_json

    def suspended_json(state):
        if state.status == "paused" and state.paused_reason == "user-interrupted (/stop)":
            writing.set()
            assert release.wait(timeout=5), "stop write was not released"
        return original_json(state)

    def claim_newer_owner():
        claiming.set()
        if claim_kind == "generation":
            return runner._begin_session_run_generation(key)
        return runner.session_store.reset_session(key)

    def set_newer_goal():
        setting.set()
        return GoalManager(session_id).set("new goal after the owner claim")

    monkeypatch.setattr(GoalState, "to_json", suspended_json)
    stop_task = asyncio.create_task(runner._handle_stop_command(MessageEvent(text="/stop", source=source)))
    claim_task = set_task = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(writing.wait, 2), timeout=3)
        claim_task = asyncio.create_task(asyncio.to_thread(claim_newer_owner))
        assert await asyncio.wait_for(asyncio.to_thread(claiming.wait, 2), timeout=3)
        if claim_kind == "generation":
            # Admission itself cannot block: the *goal writes* are serialized by SQLite instead.
            assert await asyncio.wait_for(claim_task, timeout=2) == 1
            set_task = asyncio.create_task(asyncio.to_thread(set_newer_goal))
            assert await asyncio.wait_for(asyncio.to_thread(setting.wait, 2), timeout=3)
            blocked_task = set_task
        else:
            blocked_task = claim_task  # route replacement still shares the off-loop store lock
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(blocked_task), timeout=2)
    finally:
        release.set()
        await asyncio.wait_for(stop_task, timeout=2)
        if claim_task is not None:
            await asyncio.wait_for(claim_task, timeout=2)
        if set_task is not None:
            await asyncio.wait_for(set_task, timeout=2)

    current_id = await asyncio.to_thread(runner._lookup_session_id_under_store_lock, runner.session_store, key)
    if claim_kind == "route":
        await asyncio.to_thread(lambda: GoalManager(current_id).set("new goal after the owner claim"))
    state = GoalManager(current_id).state
    assert state is not None and state.status == "active"
    assert state.goal == "new goal after the owner claim"


@pytest.mark.asyncio
async def test_delayed_stop_tail_does_not_pause_successor_goal(monkeypatch):
    runner, adapter, source, key, old_session_id = await _runner(monkeypatch)
    _active_goal(old_session_id)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def suspended_interrupt(self, _session_key, _chat_id, **_kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(type(adapter), "interrupt_session_activity", suspended_interrupt)
    stop_task = asyncio.create_task(runner._interrupt_and_clear_session(
        key, source, interrupt_reason="Stop requested", invalidation_reason="stop_command"))
    await entered.wait()

    successor = await runner.async_session_store.reset_session(key)
    _active_goal(successor.session_id)
    release.set()
    await stop_task

    from hermes_cli.goals import GoalManager
    assert GoalManager(old_session_id).state.status == "active"
    assert GoalManager(successor.session_id).state.status == "active"
