"""Real registry lifecycle: autonomous bookkeeping, durable user stops."""

import json

import pytest

from hermes_cli import goals
from tools import goal_tool  # noqa: F401 -- registers the real handler
from tools.goal_authority import goal_user_request_scope
from tools.registry import registry


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    yield
    goals._DB_CACHE.clear()


def invoke(action, *, body="", **args):
    with goal_user_request_scope("lifecycle", body):
        result = registry.dispatch(
            "set_goal", {"action": action, **args}, session_id="lifecycle",
            turn_id="current-turn",
            goal_control_revision=goals.get_goal_control_revision("lifecycle"),
            user_task="Assistant offered to resume. /goal resume",  # Not authority.
        )
    assert isinstance(result, str)
    return json.loads(result)


def saved():
    state = goals.load_goal("lifecycle")
    assert state is not None
    return state


def test_autonomous_lifecycle_without_new_user_instruction():
    assert invoke("set", goal="Ship the requested parser")["success"]
    assert invoke("subgoal_add", text="Validate malformed input")["success"]
    assert invoke("gate_add", command="true")["success"]
    assert invoke("pause", reason="Missing test fixture")["success"]
    assert not saved().user_stopped
    assert invoke("resume")["success"]
    assert invoke("edit", goal="Ship and validate the requested parser")["success"]
    goals.GoalManager("lifecycle").mark_done("Verified parser and tests")
    assert invoke("clear", reason="Remove demonstrably duplicate tracking; verified completion is retained")["success"]
    assert saved().status == "cleared"
    assert saved().last_verdict == "done"
    assert not saved().user_stopped


def test_resume_releases_active_wait_barrier():
    assert invoke("set", goal="Wait for the authorized build", max_turns=8)["success"]
    assert invoke("wait", pid=123456, reason="Build running")["success"]
    assert saved().status == "active" and saved().waiting_on_pid == 123456
    assert invoke("resume")["success"]
    assert saved().status == "active" and saved().waiting_on_pid is None
    assert saved().max_turns == 8


def test_original_clear_it_needs_no_phrase_gate():
    assert invoke("set", goal="Completed obsolete tracking")["success"]
    assert invoke("pause", reason="Obsolete")["success"]
    assert invoke("clear", body="Clear it")["success"]
    assert saved().status == "cleared"


@pytest.mark.parametrize("stop_action", ["pause", "clear"])
def test_user_stop_survives_restart_and_cannot_be_laundered(stop_action):
    assert invoke("set", goal="Finish the requested parser")["success"]
    assert invoke(stop_action, body="Stop for now", user_requested=True)["success"]
    assert saved().user_stopped
    if stop_action == "pause":
        assert saved().paused_reason == "user-paused"
    goals._DB_CACHE.clear()  # Read durable state, not the prior manager instance.
    for action, args in [("resume", {}), ("set", {"goal": "Same requested work", "replace_existing": True})]:
        denied = invoke(action, body="What's the weather?", **args)
        assert denied["error_code"] == "user_stop_requires_direction"
    # Neither clearing nor drafting can erase a prior user stop.
    if stop_action == "pause":
        assert invoke("clear")["success"]
    assert saved().user_stopped
    assert invoke("draft", goal="Same required work", contract={"verification": "Tests pass"})["success"]
    assert saved().status == "paused" and saved().user_stopped
    assert invoke("resume")["error_code"] == "user_stop_requires_direction"
    assert invoke("resume", user_requested=True)["error_code"] == "user_direction_required"
    assert invoke("resume", body="Go ahead", user_requested=True)["success"]
    assert saved().status == "active" and not saved().user_stopped


def test_user_can_stop_an_already_agent_paused_goal():
    assert invoke("set", goal="Requested work")["success"]
    assert invoke("pause", reason="Blocked")["success"]
    assert invoke("pause", body="Keep this stopped", user_requested=True)["success"]
    assert saved().user_stopped
    assert invoke("resume")["error_code"] == "user_stop_requires_direction"


@pytest.mark.parametrize("control", ["pause", "clear"])
def test_direct_manager_controls_record_user_stop(control):
    manager = goals.GoalManager("lifecycle")
    manager.set("Requested work")
    getattr(manager, control)()
    assert saved().user_stopped
    assert invoke("set", goal="Reactivation")["error_code"] == "user_stop_requires_direction"


def test_autonomous_replacement_cannot_replenish_budget():
    manager = goals.GoalManager("lifecycle")
    state = manager.set("Requested work", max_turns=8)
    state.turns_used = 5
    goals.save_goal("lifecycle", state)
    assert invoke("clear")["success"]
    assert invoke("set", goal="Remaining requested work")["success"]
    assert saved().turns_used == 5 and saved().max_turns == 8
    assert invoke("pause")["success"]
    assert invoke("resume")["success"]
    assert saved().turns_used == 5


def test_draft_never_activates_even_transiently(monkeypatch):
    original = goals.save_goal
    statuses = []

    def record(sid, state):
        statuses.append(state.status)
        return original(sid, state)

    monkeypatch.setattr(goals, "save_goal", record)
    result = invoke("draft", goal="Plan the requested migration", contract={"verification": "Migration passes"})
    assert result["success"]
    assert statuses and set(statuses) == {"paused"}
    assert not saved().user_stopped
    assert invoke("resume")["success"]


def test_legacy_user_pause_and_clear_remain_stopped():
    for data in [
        {"goal": "Work", "status": "paused", "paused_reason": "user-paused"},
        {"goal": "Work", "status": "cleared"},
    ]:
        assert goals.GoalState.from_json(json.dumps(data)).user_stopped
    agent_pause = {"goal": "Work", "status": "paused", "paused_reason": "judge API unreachable"}
    assert not goals.GoalState.from_json(json.dumps(agent_pause)).user_stopped


@pytest.mark.parametrize("stop_action", ["pause", "clear"])
def test_compression_carries_user_stop_even_without_active_tracking(stop_action):
    manager = goals.GoalManager("parent")
    manager.set("Requested work")
    getattr(manager, stop_action)()
    assert goals.migrate_goal_to_session("parent", "lifecycle", reason="compression")
    assert saved().user_stopped
    assert invoke("set", goal="Same work")["error_code"] == "user_stop_requires_direction"


def test_user_requested_flag_is_typed():
    assert invoke("set", goal="Work", body="Resume", user_requested="false")["error_code"] == "invalid_user_requested"
