"""Model-callable standing-goal lifecycle contracts."""

import json
import os
import threading

import pytest


@pytest.fixture
def isolated_goal_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield
    goals._DB_CACHE.clear()


def call_goal(**kwargs):
    from hermes_cli.goals import get_goal_control_revision
    from tools.goal_tool import set_goal_tool

    session_id = kwargs.get("session_id", "")
    if session_id:
        kwargs.setdefault("turn_id", "turn-1")
        kwargs.setdefault("goal_control_revision", get_goal_control_revision(session_id))
    result = json.loads(set_goal_tool(**kwargs))
    if result.get("success") and result.get("change"):
        assert result["state"]["goal"] in result["notice"]
        assert "/goal status" in result["notice"]
    else:
        assert "notice" not in result
    return result


def test_autonomous_set_persists_contract_and_activates(isolated_goal_db):
    from hermes_cli.goals import GoalManager

    result = call_goal(
        goal="Implement the parser",
        max_turns=7,
        contract={"verification": "Parser tests pass", "constraints": "Keep the API stable"},
        session_id="autonomous",
    )
    assert result["success"] is True
    assert result["persisted"] is True
    assert result["status"] == "active"
    state = GoalManager("autonomous").state
    assert state is not None
    assert state.goal == "Implement the parser"
    assert state.max_turns == 7
    assert state.contract.verification == "Parser tests pass"
    assert state.contract.constraints == "Keep the API stable"


@pytest.mark.parametrize(
    "goal",
    [
        "fix tools/parser.py and verify the tests",
        "upgrade to version 3.2 and verify compatibility",
        "check https://example.com/docs and archive the result",
    ],
)
def test_goal_payload_preserves_dotted_tokens(isolated_goal_db, goal):
    result = call_goal(goal=goal, session_id=f"dotted-{abs(hash(goal))}")
    assert result["success"] is True
    assert result["state"]["goal"] == goal


def test_edit_cannot_erase_existing_constraint(isolated_goal_db):
    call_goal(
        goal="Audit backups",
        contract={"constraints": "Keep production intact"},
        session_id="edit-constraint",
    )
    result = call_goal(
        action="edit",
        goal="Verify backup recoverability",
        contract={"constraints": ""},
        session_id="edit-constraint",
    )
    assert result["error_code"] == "invalid_edit"
    state = call_goal(action="status", session_id="edit-constraint")["state"]
    assert state["contract"]["constraints"] == "Keep production intact"


def test_status_does_not_disclose_writing_guidance(isolated_goal_db):
    result = call_goal(action="status", session_id="inspect")
    assert result["state"] is None
    assert "guidance" not in result
    guide = call_goal(action="guide", session_id="inspect")
    assert guide["state"] is None
    assert "guidance" in guide
    assert call_goal(action="status", session_id="inspect")["state"] is None


def test_goal_contract_rejects_invalid_field_types(isolated_goal_db):
    result = call_goal(
        goal="Review the document",
        contract={"constraints": ["bad type"]},
        session_id="invalid-contract",
    )
    assert result["error_code"] == "invalid_contract"


def test_missing_turn_scope_fails_closed(isolated_goal_db):
    result = call_goal(goal="Implement and verify", session_id="missing-turn", turn_id="")
    assert result["success"] is False
    assert result["error_code"] == "missing_turn_scope"


def test_replacement_requires_explicit_structural_flag(isolated_goal_db):
    from hermes_cli.goals import GoalManager

    GoalManager("replace").set("Keep this goal")
    blocked = call_goal(goal="New goal", session_id="replace")
    assert blocked["error_code"] == "active_goal_exists"
    replaced = call_goal(goal="New goal", replace_existing=True, session_id="replace")
    assert replaced["success"] is True
    assert replaced["replaced_existing"] is True
    assert replaced["replaced_goal"] == "Keep this goal"
    state = GoalManager("replace").state
    assert state is not None and state.goal == "New goal"


def test_persistence_failure_and_missing_scope_fail_closed(isolated_goal_db, monkeypatch):
    from hermes_cli import goals

    missing = call_goal(goal="Goal", session_id="")
    assert missing["error_code"] == "missing_session_scope"
    monkeypatch.setattr(goals, "save_goal", lambda *_args, **_kwargs: False)
    failed = call_goal(goal="Unpersisted goal", session_id="write-fail")
    assert failed["success"] is False
    assert failed["persisted"] is False
    assert failed["error_code"] == "goal_persistence_failed"


def test_refresh_based_mutations_fail_when_persistence_is_not_confirmed(
    isolated_goal_db, monkeypatch
):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    GoalManager("write-fail-subgoal").set("Keep persisted subgoals")
    GoalManager("write-fail-gate").set("Keep persisted gates")
    waiting = GoalManager("write-fail-unwait")
    waiting.set("Keep the persisted wait barrier")
    waiting.wait_on(os.getpid())
    monkeypatch.setattr(goals, "save_goal", lambda *_args, **_kwargs: False)

    cases = [
        ("write-fail-subgoal", "subgoal_add", {"text": "Unpersisted criterion"}),
        ("write-fail-gate", "gate_add", {"command": "true"}),
        ("write-fail-unwait", "unwait", {}),
    ]
    for session_id, action, kwargs in cases:
        result = call_goal(action=action, session_id=session_id, **kwargs)
        assert result["success"] is False
        assert result["error_code"] == "goal_persistence_failed"
        assert result["persisted"] is False


def test_cached_manager_refreshes_after_tool_write(isolated_goal_db):
    from hermes_cli.goals import GoalManager

    cached = GoalManager("refresh")
    assert cached.is_active() is False
    result = call_goal(goal="Visible to cached manager", session_id="refresh")
    assert result["success"] is True
    assert cached.is_active() is True
    assert cached.state is not None and cached.state.goal == "Visible to cached manager"


def test_persisted_control_revision_blocks_stale_activation(isolated_goal_db):
    from hermes_cli import goals
    from hermes_cli.goals import advance_goal_control_revision, get_goal_control_revision

    expected = get_goal_control_revision("fenced")
    advance_goal_control_revision("fenced")
    goals._MODEL_GOAL_CONTROL_REVISIONS.clear()
    goals._DB_CACHE.clear()
    result = call_goal(
        goal="Must not reactivate",
        session_id="fenced",
        turn_id="turn-1",
        goal_control_revision=expected,
    )
    assert result["success"] is False
    assert result["error_code"] == "goal_activation_cancelled"


def test_control_revision_race_preserves_later_user_action(isolated_goal_db):
    from hermes_cli.goals import GoalManager, advance_goal_control_revision, get_goal_control_revision

    session_id = "race"
    expected = get_goal_control_revision(session_id)
    release = threading.Event()
    finished = threading.Event()
    result = {}

    def delayed_activation():
        release.wait(timeout=2)
        result.update(call_goal(
            goal="Stale activation", session_id=session_id, turn_id="turn-1",
            goal_control_revision=expected,
        ))
        finished.set()

    thread = threading.Thread(target=delayed_activation)
    thread.start()
    advance_goal_control_revision(session_id)
    GoalManager(session_id).clear()
    release.set()
    assert finished.wait(timeout=2)
    thread.join(timeout=2)
    assert result["error_code"] == "goal_activation_cancelled"
    state = GoalManager(session_id).state
    assert state is None or state.status == "cleared"


def test_surface_and_schema_expose_autonomous_goal_control():
    from model_tools import get_tool_definitions
    from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS

    def names(enabled, disabled=None):
        return {item["function"]["name"] for item in get_tool_definitions(
            enabled_toolsets=enabled, disabled_toolsets=disabled, quiet_mode=True,
            skip_tool_search_assembly=True,
        )}

    assert "set_goal" in names(["hermes-cli"])
    assert "set_goal" in names(["hermes-telegram"])
    assert "set_goal" not in names(["coding"])
    assert "set_goal" not in names(["hermes-cron"])
    assert "set_goal" not in names(["hermes-telegram"], ["goal"])
    definition = next(item["function"] for item in get_tool_definitions(
        enabled_toolsets=["hermes-cli"], quiet_mode=True, skip_tool_search_assembly=True,
    ) if item["function"]["name"] == "set_goal")
    properties = definition["parameters"]["properties"]
    assert definition["parameters"]["required"] == ["action"]
    assert "authorization_text" not in properties
    assert "user_requested" in properties
    assert "set_goal" in DELEGATE_BLOCKED_TOOLS


def test_writing_guidance_is_progressively_disclosed(isolated_goal_db):
    from tools.goal_tool import GOAL_ACTIONS, GOAL_WRITING_GUIDANCE, SET_GOAL_SCHEMA

    assert {"guide", "edit", "set", "draft", "status", "pause", "resume"} <= set(GOAL_ACTIONS)
    ambient = json.dumps(SET_GOAL_SCHEMA)
    assert GOAL_WRITING_GUIDANCE not in ambient
    assert "Decide autonomously" in ambient
    assert "action='guide'" in ambient
    assert "verbatim request or implementation plan" not in ambient
    result = call_goal(action="guide", session_id="disclosure")
    assert result["guidance"] == GOAL_WRITING_GUIDANCE
    assert result["state"] is None


def test_routine_mutations_need_only_trusted_turn_scope(isolated_goal_db):
    from hermes_cli.goals import GoalManager

    manager = GoalManager("parity")
    manager.set("Existing")
    manager.add_subgoal("Coverage")
    manager.add_gate("true")
    assert call_goal(action="status", session_id="parity")["state"]["goal"] == "Existing"
    assert call_goal(action="subgoal_list", session_id="parity")["items"] == ["Coverage"]
    assert call_goal(action="gate_list", session_id="parity")["items"][0]["command"] == "true"

    cases = [
        ("wait", {"pid": os.getpid()}),
        ("unwait", {}),
        ("subgoal_add", {"text": "Regression tests"}),
        ("subgoal_remove", {"index": 1}),
        ("subgoal_clear", {}),
        ("gate_add", {"command": "true"}),
        ("gate_remove", {"index": 1}),
        ("gate_clear", {}),
    ]
    for action, kwargs in cases:
        current = GoalManager("parity").state
        assert current is not None
        if action == "subgoal_remove" and not current.subgoals:
            GoalManager("parity").add_subgoal("Coverage")
        if action == "gate_remove" and not current.gates:
            GoalManager("parity").add_gate("true")
        assert call_goal(action=action, session_id="parity", **kwargs)["success"] is True


def test_resume_preserves_budget_and_done_is_absorbing(isolated_goal_db):
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("budget")
    state = mgr.set("Budget", max_turns=8)
    state.turns_used = 5
    mgr._persist_state(state)
    mgr.pause(user_requested=False)
    resumed = call_goal(action="resume", session_id="budget")
    assert resumed["state"]["turns_used"] == 5
    mgr.mark_done("verified")
    rejected = call_goal(action="resume", session_id="budget")
    assert rejected["error_code"] == "invalid_goal_transition"


def test_draft_persists_a_paused_goal(isolated_goal_db):
    from hermes_cli.goals import GoalManager

    result = call_goal(
        action="draft", goal="Ship safely", contract={"verification": "Tests pass"},
        session_id="draft",
    )
    assert result["success"] is True
    assert result["state"]["status"] == "paused"
    persisted = GoalManager("draft").state
    assert persisted is not None
    assert persisted.status == "paused"
    assert persisted.contract.verification == "Tests pass"


def test_goal_action_labels_are_semantic():
    from agent.display import build_tool_label, set_friendly_tool_labels

    set_friendly_tool_labels(True)
    assert build_tool_label("set_goal", {"action": "pause"}) == "Pausing goal"
    assert build_tool_label("set_goal", {"action": "subgoal_add", "text": "Tests"}) == "Adding subgoal Tests"
    assert build_tool_label("set_goal", {"action": "gate_add", "command": "pytest"}) == "Adding quality gate pytest"


def test_clear_requires_confirmed_cleared_readback(isolated_goal_db, monkeypatch):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    GoalManager("clear-fail").set("Existing")
    monkeypatch.setattr(goals, "save_goal", lambda *_args, **_kwargs: False)
    result = call_goal(action="clear", session_id="clear-fail")
    assert result["error_code"] == "goal_persistence_failed"
    persisted = goals.load_goal("clear-fail")
    assert persisted is not None and persisted.status == "active"


def test_clear_can_remove_a_completed_goal(isolated_goal_db):
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("done-clear")
    mgr.set("Finished")
    mgr.mark_done("verified")
    result = call_goal(action="clear", session_id="done-clear")
    assert result["success"] is True
    assert result["state"]["status"] == "cleared"
