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
    assert definition["parameters"]["required"] == ["goal", "authorization_text"]

    from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS

    assert "set_goal" in DELEGATE_BLOCKED_TOOLS
