"""Recovery for goal continuation turns that end without a usable response."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import goals


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(home))
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()
    reset_hermes_home_override(token)


def test_empty_continuation_rearms_durable_timed_wait(hermes_home):
    manager = goals.GoalManager("empty-continuation")
    manager.set("finish the work")
    assert manager.state is not None
    instance = manager.state.created_at

    assert manager.rearm_after_empty_continuation(expected_instance=instance) == "rearmed"
    state = goals.load_goal("empty-continuation")
    assert state.status == "active"
    assert state.waiting_reason == "continuation turn produced no response"
    assert state.waiting_since > 0
    assert state.waiting_until >= state.waiting_since + 59
    assert state.consecutive_continuation_failures == 1
    # A restart/reload sees the same wait and the watcher can admit it.
    reloaded = goals.GoalManager("empty-continuation")
    assert reloaded.is_parked()
    assert json.loads(reloaded.state.to_json())["waiting_until"] == state.waiting_until


def test_rearm_is_idempotent_and_fenced(hermes_home):
    manager = goals.GoalManager("empty-fences")
    manager.set("finish the work")
    assert manager.state is not None
    instance = manager.state.created_at
    assert manager.rearm_after_empty_continuation(expected_instance=instance) == "rearmed"
    wait_until = goals.load_goal("empty-fences").waiting_until

    # A duplicate settlement cannot extend the existing retry or create a second one.
    duplicate = goals.GoalManager("empty-fences")
    assert duplicate.rearm_after_empty_continuation(expected_instance=instance) is None
    assert goals.load_goal("empty-fences").waiting_until == wait_until
    # A continuation for another goal instance cannot arm the current goal.
    replacement = goals.GoalManager("empty-fences")
    replacement.stop_waiting()
    assert replacement.rearm_after_empty_continuation(expected_instance=instance - 1) is None
    assert goals.load_goal("empty-fences").waiting_since == 0


def test_successful_continuation_control_does_not_rearm(hermes_home):
    manager = goals.GoalManager("successful-continuation")
    manager.set("finish the work")
    # A successful continuation is judged normally; this recovery primitive is never called.
    assert manager.rearm_after_empty_continuation(expected_instance=manager.state.created_at) == "rearmed"
    manager.stop_waiting()
    assert goals.load_goal("successful-continuation").waiting_since == 0


def test_user_preemption_is_skipped_by_gateway_recovery(monkeypatch):
    """The gateway hook's preemption fence is explicit and does not need goal state access."""
    import asyncio
    from types import SimpleNamespace
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run_goals import GatewayGoalsMixin

    event = MessageEvent(text="continuation", message_type=MessageType.TEXT, metadata={"goal_continuation": True})
    runner = object.__new__(GatewayGoalsMixin)
    runner._delivery_adapter_for = lambda source: None
    runner._session_key_for_source = lambda source: "session-key"
    runner._session_invalidation_epoch = lambda key: 0
    session = SimpleNamespace(origin=SimpleNamespace(), session_id="preempted")
    called = False

    async def never(*args, **kwargs):
        nonlocal called
        called = True

    runner._run_in_executor_with_context = never
    result = asyncio.run(runner._rearm_failed_goal_continuation(
        event=event,
        agent_result={"interrupted": True, "interrupt_source": "gateway_user_turn_preemption"},
        session_entry=session,
    ))
    assert result is None and not called


def test_empty_continuation_pauses_at_bound(hermes_home):
    manager = goals.GoalManager("empty-bound")
    manager.set("finish the work")
    assert manager.state is not None
    instance = manager.state.created_at
    for _ in range(2):
        assert manager.rearm_after_empty_continuation(expected_instance=instance) == "rearmed"
        manager.stop_waiting()
    assert manager.rearm_after_empty_continuation(expected_instance=instance) == "paused"
    state = goals.load_goal("empty-bound")
    assert state.status == "paused"
    assert state.paused_reason == "continuation turn produced no response 3 times in a row"
    assert state.waiting_since == 0
