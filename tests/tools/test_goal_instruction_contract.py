"""The model-facing contract must distinguish completion from tracking removal.

These assertions inspect delivered instructions, not source or full-text snapshots.
They guard safety-critical clauses; they do not prove stochastic model compliance.
"""

import json

from hermes_cli import goals
from model_tools import get_tool_definitions
from tools.registry import registry


def test_exposed_schema_and_guide_preserve_completion_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    definition = next(item["function"] for item in get_tool_definitions(
        enabled_toolsets=["goal"], quiet_mode=True, skip_tool_search_assembly=True,
    ) if item["function"]["name"] == "set_goal")
    guide = json.loads(registry.dispatch(
        "set_goal", {"action": "guide"}, session_id="instruction-contract",
    ))
    assert guide["success"] and guide["state"] is None
    for delivered in (definition["description"], guide["guidance"]):
        text = delivered.lower()
        assert "clear is not completion" in text
        assert "do not routinely clear completed records" in text
        assert "retained tracking or recorded verified completion" in text
        assert "before autonomous clear" in text
        assert "lifecycle failure" in text
        assert "user-required work" in text
    properties = definition["parameters"]["properties"]
    assert "distinct from successful completion" in properties["contract"]["properties"]["stop_when"]["description"]
    assert "no new user request" in properties["user_requested"]["description"]
    assert "evidence" in properties["replace_existing"]["description"]
    assert "reset limits" in properties["replace_existing"]["description"]
    assert "guidance" not in json.loads(registry.dispatch(
        "set_goal", {"action": "status"}, session_id="instruction-contract",
    ))


def test_persisted_goal_continuations_keep_full_completion_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    manager = goals.GoalManager("continuation-contract")
    manager.set("Deliver the requested migration")
    for variant in ("plain", "subgoal", "contract"):
        if variant == "subgoal":
            manager.add_subgoal("Verify production readback")
        elif variant == "contract":
            manager.edit(manager.state.goal, contract=goals.GoalContract(
                verification="Reconcile all migrated records",
                boundaries="Staging only until approval",
            ))
        prompt = goals.GoalManager(manager.session_id).next_continuation_prompt()
        assert manager.state.goal in prompt
        assert "all subgoals and gates" in prompt
        assert "Never clear tracking" in prompt
        assert "normal evaluator" in prompt
        assert "lifecycle failure" in prompt
        if variant != "plain":
            assert "Verify production readback" in prompt
        if variant == "contract":
            assert "Reconcile all migrated records" in prompt
            assert "Staging only until approval" in prompt
    assert goals.load_goal(manager.session_id).status == "active"
