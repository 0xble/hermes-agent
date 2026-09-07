"""Explicit control requests retain current-turn and action boundaries."""

import json

import pytest

from hermes_cli import goals
from tools import goal_tool  # Registers the real tool handler.
from tools.registry import registry
from tools.goal_authority import goal_user_request_scope


@pytest.mark.parametrize("action", ["clear", "pause", "resume"])
@pytest.mark.parametrize("slash", [False, True])
def test_explicit_control_persists_through_registry(tmp_path, monkeypatch, action, slash):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = goals.GoalManager("control")
    manager.set("Keep the parser working")
    if action in {"clear", "resume"}:
        manager.pause()
    user_text = f"/goal {action}" if slash else f"{action} the goal"
    with goal_user_request_scope("control", user_text):
        result = json.loads(registry.dispatch("set_goal",
            {"action": action, "authorization_text": user_text},
            session_id="control", turn_id="turn",
            goal_control_revision=goals.get_goal_control_revision("control"),
        ))
    assert result["success"] is True
    persisted = goals.load_goal("control")
    assert persisted.status == {"clear": "cleared", "pause": "paused", "resume": "active"}[action]


@pytest.mark.parametrize("user_text,span", [
    ("Clear it", "Clear it"),
    ("yes", "yes"),
    ("do it", "do it"),
    ("/goal pause", "/goal pause"),
    ("/goal clear extra", "/goal clear"),
    ("/goal clear. Actually, don't.", "/goal clear"),
    ("Explain /goal clear", "/goal clear"),
    ("Should I use /goal clear?", "/goal clear"),
    ("Don't /goal clear", "/goal clear"),
    ('"/goal clear"', "/goal clear"),
    ("```\n/goal clear\n```", "/goal clear"),
    ("Clear it", "clear the goal"),
    ("", "/goal clear"),
])
def test_clear_cannot_borrow_or_infer_authority(tmp_path, monkeypatch, user_text, span):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = goals.GoalManager("denied")
    manager.set("Keep the parser working")
    manager.pause()
    before = goals.load_goal("denied").to_json()
    with goal_user_request_scope("denied", user_text):
        result = json.loads(registry.dispatch("set_goal",
            {"action": "clear", "authorization_text": span},
            user_task="clear the goal",  # Decorated/history input is not authority.
            session_id="denied", turn_id="turn",
            goal_control_revision=goals.get_goal_control_revision("denied"),
        ))
    assert result["success"] is False
    assert goals.load_goal("denied").to_json() == before
