import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    source = Path(__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location("goal_lifecycle_test_plugin", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    yield module
    goals._DB_CACHE.clear()


def call(plugin, args):
    return json.loads(plugin.goal_set(args, task_id="slice3-test"))


def test_set_additive_subgoal_status_and_persistence(plugin):
    result = call(plugin, {
        "action": "set",
        "goal": "Prove goal lifecycle",
        "verification": "Plugin test passes",
        "constraints": "No production effects",
    })
    assert result["success"] is True
    assert result["persisted"] is True
    assert result["state"]["status"] == "active"

    result = call(plugin, {"action": "subgoal_add", "text": "Read back state"})
    assert result["success"] is True
    assert result["state"]["subgoals"] == ["Read back state"]

    result = call(plugin, {"action": "status"})
    assert result["state"]["goal"] == "Prove goal lifecycle"
    assert result["state"]["subgoals"] == ["Read back state"]


@pytest.mark.parametrize("action", ["pause", "resume", "clear", "edit"])
def test_user_only_actions_are_rejected(plugin, action):
    result = call(plugin, {"action": action})
    assert result["success"] is False
    assert result["error_code"] == "user_control_only"


def test_delegated_child_is_parent_only(plugin):
    from agent.delegation_context import delegated_child_context
    with delegated_child_context("child"):
        result = call(plugin, {"action": "set", "goal": "must refuse"})
    assert result == {
        "error": "goal_set is available only to the owning parent session",
        "error_code": "parent_only",
        "success": False,
    }



def test_enrollment_lands_in_the_slash_command_namespace(plugin, monkeypatch):
    """The tool must write the goal the gateway's own /goal commands read.

    model_tools passes ``session_id`` and ``task_id`` to every tool handler. Goals are keyed
    ``goal:<session_id>`` in SessionDB state_meta, so enrolling under ``task_id`` (a delegation
    id in some paths) would create a goal no user-facing command can see or clear.
    """
    from hermes_cli.goals import GoalManager, load_goal

    result = json.loads(plugin.goal_set(
        {"action": "set", "goal": "Namespace check", "verification": "load_goal returns it"},
        session_id="session-abc", task_id="task-zzz",
    ))
    assert result["success"] is True

    # The exact loader the slash command uses.
    persisted = load_goal("session-abc")
    assert persisted is not None and persisted.goal == "Namespace check"
    assert GoalManager("session-abc").has_goal() is True
    # Nothing was written under the task id.
    assert load_goal("task-zzz") is None


def test_session_scope_is_required_and_never_model_supplied(plugin):
    """No trusted scope means refusal, and a model-supplied session id is ignored."""
    refused = json.loads(plugin.goal_set({"action": "set", "goal": "no scope"}))
    assert refused["error_code"] == "missing_session_scope"

    spoofed = json.loads(plugin.goal_set(
        {"action": "set", "goal": "spoofed", "session_id": "attacker"}, session_id="real-session"))
    assert spoofed["success"] is True
    from hermes_cli.goals import load_goal
    assert load_goal("attacker") is None
    assert load_goal("real-session") is not None
