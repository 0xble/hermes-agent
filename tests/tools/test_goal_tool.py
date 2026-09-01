"""Model-callable standing-goal activation."""

import json
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
    from tools.goal_tool import set_goal

    session_id = kwargs.get("session_id", "")
    if session_id:
        kwargs.setdefault("turn_id", "turn-1")
        kwargs.setdefault(
            "goal_control_revision",
            get_goal_control_revision(session_id),
        )
    if kwargs.get("user_task") is not None:
        kwargs.setdefault("authorization_text", kwargs["user_task"])
    return json.loads(set_goal(**kwargs))


def test_explicit_request_persists_contract_and_activates(isolated_goal_db):
    from hermes_cli.goals import GoalManager
    result = call_goal(
        goal="Implement the parser",
        max_turns=7,
        contract={"verification": "Parser tests pass", "constraints": "Keep the API stable"},
        session_id="explicit",
        user_task="Set a goal to implement the parser, then validate it works.",
    )
    assert result["success"] is True
    assert result["persisted"] is True
    assert result["status"] == "active"
    assert "continue working" in result["message"].lower()
    state = GoalManager("explicit").state
    assert state is not None
    assert state.goal == "Implement the parser"
    assert state.max_turns == 7
    assert state.contract.verification == "Parser tests pass"
    assert state.contract.constraints == "Keep the API stable"


@pytest.mark.parametrize("user_task", [
    None,
    "Implement the parser and validate it.",
    "Recommend a goal for this project.",
    "Draft a goal, but do not activate it.",
])
def test_activation_requires_explicit_authorization(isolated_goal_db, user_task):
    result = call_goal(goal="Implement the parser", session_id="auth", user_task=user_task)
    assert result["success"] is False
    assert result["error_code"] == "explicit_goal_authorization_required"


@pytest.mark.parametrize(
    ("user_task", "authorization_text", "error_code"),
    [
        (
            "Set a goal to implement this.\nThen validate it works.",
            "Set a goal to implement this.",
            None,
        ),
        (
            "Set a goal to implement this, then validate it works!",
            "Set a goal to implement this, then validate it works!",
            None,
        ),
        (
            "Set a goal to implement this.",
            "Set a goal from an earlier turn.",
            "authorization_not_in_current_turn",
        ),
        (
            "Should this be a goal?",
            "Should this be a goal?",
            "explicit_goal_authorization_required",
        ),
        (
            "Recommend whether to set a goal for this work.",
            "set a goal",
            "explicit_goal_authorization_required",
        ),
        (
            "Don't change the goal.",
            "Don't change the goal.",
            "explicit_goal_authorization_required",
        ),
        (
            "Never replace my goal.",
            "Never replace my goal.",
            "explicit_goal_authorization_required",
        ),
        (
            "Do not set a goal for this.",
            "set a goal for this",
            "explicit_goal_authorization_required",
        ),
    ],
)
def test_authorization_span_and_direct_instruction(
    isolated_goal_db,
    user_task,
    authorization_text,
    error_code,
):
    result = call_goal(
        goal="Implement and verify",
        session_id=f"auth-span-{abs(hash(user_task))}",
        user_task=user_task,
        authorization_text=authorization_text,
    )
    assert result["success"] is (error_code is None)
    if error_code is not None:
        assert result["error_code"] == error_code


def test_missing_turn_scope_fails_closed(isolated_goal_db):
    result = call_goal(
        goal="Implement and verify",
        session_id="missing-turn",
        turn_id="",
        user_task="Set a goal to implement and verify.",
    )
    assert result["success"] is False
    assert result["error_code"] == "missing_turn_scope"


def test_replacement_is_conspicuous(isolated_goal_db):
    from hermes_cli.goals import GoalManager
    GoalManager("replace").set("Keep this goal")
    blocked = call_goal(
        goal="New goal",
        session_id="replace",
        user_task="Set a goal to do something else.",
    )
    assert blocked["error_code"] == "active_goal_exists"
    blocked = call_goal(
        goal="New goal",
        replace_existing=True,
        session_id="replace",
        user_task="Set a goal to do something else.",
    )
    assert blocked["error_code"] == "explicit_replacement_authorization_required"
    replaced = call_goal(
        goal="New goal",
        replace_existing=True,
        session_id="replace",
        user_task="Replace the active goal with a goal to ship the parser.",
    )
    assert replaced["success"] is True
    assert replaced["replaced_existing"] is True
    assert replaced["replaced_goal"] == "Keep this goal"
    assert replaced["goal"] == "New goal"
    state = GoalManager("replace").state
    assert state is not None and state.goal == "New goal"


def test_persistence_failure_and_missing_scope_fail_closed(isolated_goal_db, monkeypatch):
    from hermes_cli import goals
    missing = call_goal(goal="Goal", session_id="", user_task="Set a goal to test scope.")
    assert missing["error_code"] == "missing_session_scope"
    monkeypatch.setattr(goals, "save_goal", lambda *_args, **_kwargs: False)
    failed = call_goal(
        goal="Unpersisted goal",
        session_id="write-fail",
        user_task="Set a goal to test failed persistence.",
    )
    assert failed["success"] is False
    assert failed["persisted"] is False
    assert failed["error_code"] == "goal_persistence_failed"


def test_refresh_based_mutations_fail_when_persistence_is_not_confirmed(
    isolated_goal_db, monkeypatch
):
    import os

    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    GoalManager("write-fail-subgoal").set("Keep persisted subgoals")
    GoalManager("write-fail-gate").set("Keep persisted gates")
    waiting = GoalManager("write-fail-unwait")
    waiting.set("Keep the persisted wait barrier")
    waiting.wait_on(os.getpid())
    monkeypatch.setattr(goals, "save_goal", lambda *_args, **_kwargs: False)

    cases = [
        (
            "write-fail-subgoal",
            "subgoal_add",
            "Add a subgoal to the active goal.",
            {"text": "Unpersisted criterion"},
        ),
        (
            "write-fail-gate",
            "gate_add",
            "Add a quality gate to the active goal.",
            {"command": "true"},
        ),
        (
            "write-fail-unwait",
            "unwait",
            "Clear the goal wait barrier.",
            {},
        ),
    ]
    for session_id, action, authorization, kwargs in cases:
        result = call_goal(
            action=action,
            session_id=session_id,
            user_task=authorization,
            **kwargs,
        )
        assert result["success"] is False
        assert result["error_code"] == "goal_persistence_failed"
        assert result["persisted"] is False


def test_cached_manager_refreshes_after_tool_write(isolated_goal_db):
    from hermes_cli.goals import GoalManager
    cached = GoalManager("refresh")
    assert cached.is_active() is False
    result = call_goal(
        goal="Visible to cached manager",
        session_id="refresh",
        user_task="Set a goal to verify cached state refresh.",
    )
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
        user_task="Set a goal to test the control fence.",
    )
    assert result["success"] is False
    assert result["error_code"] == "goal_activation_cancelled"


def test_control_revision_race_preserves_later_user_action(isolated_goal_db):
    from hermes_cli.goals import (
        GoalManager,
        advance_goal_control_revision,
        get_goal_control_revision,
    )

    session_id = "race"
    expected = get_goal_control_revision(session_id)
    release = threading.Event()
    finished = threading.Event()
    result = {}

    def delayed_activation():
        release.wait(timeout=2)
        result.update(
            call_goal(
                goal="Stale activation",
                session_id=session_id,
                turn_id="turn-1",
                goal_control_revision=expected,
                user_task="Set a goal to test stale activation.",
            )
        )
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


def test_surface_is_interactive_only_and_disableable():
    from model_tools import get_tool_definitions
    def names(enabled, disabled=None):
        return {item["function"]["name"] for item in get_tool_definitions(
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )}
    assert "set_goal" in names(["hermes-cli"])
    assert "set_goal" in names(["hermes-telegram"])
    assert "set_goal" not in names(["coding"])
    assert "set_goal" not in names(["hermes-cron"])
    assert "set_goal" not in names(["hermes-telegram"], ["goal"])

    definition = next(
        item["function"]
        for item in get_tool_definitions(
            enabled_toolsets=["hermes-cli"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        if item["function"]["name"] == "set_goal"
    )
    assert definition["parameters"]["required"] == ["action"]

    from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS

    assert "set_goal" in DELEGATE_BLOCKED_TOOLS


def test_schema_exposes_all_user_facing_actions_and_goal_writing_guidance():
    from tools.goal_tool import GOAL_ACTIONS, SET_GOAL_SCHEMA

    assert set(GOAL_ACTIONS) == {
        "set", "draft", "show", "status", "pause", "resume", "clear",
        "wait", "unwait", "subgoal_list", "subgoal_add", "subgoal_remove",
        "subgoal_clear", "gate_list", "gate_add", "gate_remove", "gate_clear",
    }
    description = SET_GOAL_SCHEMA["description"]
    for phrase in ("one concise outcome", "verification", "constraints", "boundaries", "stop_when"):
        assert phrase in description


def test_read_actions_and_mutation_parity(isolated_goal_db):
    import os
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("parity")
    mgr.set("Existing")
    mgr.add_subgoal("Coverage")
    mgr.add_gate("true")
    assert call_goal(action="status", session_id="parity")["state"]["goal"] == "Existing"
    assert call_goal(action="subgoal_list", session_id="parity")["items"] == ["Coverage"]
    assert call_goal(action="gate_list", session_id="parity")["items"][0]["command"] == "true"

    cases = [
        ("wait", "Park the active goal on this process.", {"pid": os.getpid()}),
        ("unwait", "Clear the goal wait barrier.", {}),
        ("subgoal_add", "Add a subgoal requiring regression tests.", {"text": "Regression tests"}),
        ("subgoal_remove", "Remove subgoal 1 from the active goal.", {"index": 1}),
        ("subgoal_clear", "Clear all subgoals from the active goal.", {}),
        ("gate_add", "Add a quality gate to the active goal.", {"command": "true"}),
        ("gate_remove", "Remove quality gate 1 from the active goal.", {"index": 1}),
        ("gate_clear", "Clear all quality gates from the active goal.", {}),
    ]
    for action, request, kwargs in cases:
        current = GoalManager("parity").state
        assert current is not None
        if action == "subgoal_remove" and not current.subgoals:
            GoalManager("parity").add_subgoal("Coverage")
        if action == "gate_remove" and not current.gates:
            GoalManager("parity").add_gate("true")
        denied = call_goal(action=action, session_id="parity", **kwargs)
        assert denied["error_code"] == "explicit_goal_authorization_required"
        assert call_goal(action=action, session_id="parity", user_task=request, **kwargs)["success"] is True


@pytest.mark.parametrize(
    "authorization",
    [
        "Remove subgoal 1 from the active goal.",
        "Clear all subgoals from the active goal.",
        "Remove quality gate 1 from the active goal.",
        "Clear the goal wait barrier.",
        "Clear the goal's subgoals.",
        "Remove the goal's subgoal 2.",
        "Clear the goal’s quality gates.",
    ],
)
def test_clear_rejects_narrower_goal_action_authorization(
    isolated_goal_db, authorization
):
    from hermes_cli.goals import GoalManager

    manager = GoalManager("clear-specificity")
    manager.set("Keep the standing goal")

    result = call_goal(
        action="clear",
        session_id="clear-specificity",
        user_task=authorization,
    )

    assert result["success"] is False
    assert result["error_code"] == "explicit_goal_authorization_required"
    assert manager.state is not None
    assert manager.state.goal == "Keep the standing goal"
    assert manager.state.status == "active"


def test_resume_preserves_budget_and_done_is_absorbing(isolated_goal_db):
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("budget")
    state = mgr.set("Budget", max_turns=8)
    state.turns_used = 5
    mgr._persist_state(state)
    mgr.pause()
    resumed = call_goal(action="resume", session_id="budget", user_task="Resume the active goal.")
    assert resumed["state"]["turns_used"] == 5
    mgr.mark_done("verified")
    rejected = call_goal(action="resume", session_id="budget", user_task="Resume the active goal.")
    assert rejected["error_code"] == "invalid_goal_transition"


def test_goal_action_labels_are_semantic():
    from agent.display import build_tool_label, set_friendly_tool_labels

    set_friendly_tool_labels(True)
    assert build_tool_label("set_goal", {"action": "pause"}) == "Pausing goal"
    assert build_tool_label("set_goal", {"action": "subgoal_add", "text": "Tests"}) == "Adding subgoal Tests"
    assert build_tool_label("set_goal", {"action": "gate_add", "command": "pytest"}) == "Adding quality gate pytest"


@pytest.mark.parametrize(
    "user_text, span",
    [
        ("Explain how to pause the goal.", "pause the goal"),
        ("Explain why you should pause the goal.", "pause the goal"),
        ("Pause the goal. Actually, don't pause it.", "Pause the goal"),
        ("Pause the goal. Actually, don't.", "Pause the goal"),
    ],
)
def test_mutations_reject_non_direct_or_later_revoked_authority(
    isolated_goal_db, user_text, span
):
    from hermes_cli.goals import GoalManager

    GoalManager("revoked").set("Existing")
    result = call_goal(
        action="pause",
        session_id="revoked",
        user_task=user_text,
        authorization_text=span,
    )
    assert result["error_code"] == "explicit_goal_authorization_required"
    state = GoalManager("revoked").state
    assert state is not None and state.status == "active"


def test_draft_requires_draft_authority_and_contract(isolated_goal_db):
    denied = call_goal(
        action="draft",
        goal="Ship",
        contract={"verification": "Tests pass"},
        session_id="draft-denied",
        user_task="Set a goal to ship.",
    )
    assert denied["error_code"] == "explicit_goal_authorization_required"
    allowed = call_goal(
        action="draft",
        goal="Ship",
        contract={"verification": "Tests pass"},
        session_id="draft-allowed",
        user_task="Draft and set a goal to ship.",
    )
    assert allowed["success"] is True


def test_draft_rejects_an_explanatory_request(isolated_goal_db):
    result = call_goal(
        action="draft",
        goal="Ship safely",
        contract={"verification": "Tests pass"},
        session_id="draft-explanation",
        authorization_text="Draft an explanation of how to set a goal",
        user_task="Draft an explanation of how to set a goal.",
    )
    assert result["success"] is False
    assert result["error_code"] == "explicit_goal_authorization_required"


def test_clear_requires_confirmed_cleared_readback(isolated_goal_db, monkeypatch):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    GoalManager("clear-fail").set("Existing")
    monkeypatch.setattr(goals, "save_goal", lambda *_args, **_kwargs: False)
    result = call_goal(
        action="clear",
        session_id="clear-fail",
        user_task="Clear the active goal.",
    )
    assert result["error_code"] == "goal_persistence_failed"
    persisted = goals.load_goal("clear-fail")
    assert persisted is not None and persisted.status == "active"


def test_clear_can_remove_a_completed_goal(isolated_goal_db):
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("done-clear")
    mgr.set("Finished")
    mgr.mark_done("verified")
    result = call_goal(
        action="clear",
        session_id="done-clear",
        user_task="Clear the completed goal.",
    )
    assert result["success"] is True
    assert result["state"]["status"] == "cleared"
