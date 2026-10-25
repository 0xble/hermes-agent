"""Goal visibility must not stop TUI evaluation or continuation."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("enabled", [False, True])
def test_tui_goal_notice_policy_preserves_continuation(monkeypatch, enabled):
    from hermes_cli import goal_notices, goals
    from tui_gateway import server

    emitted = []
    monkeypatch.setattr(goal_notices, "goal_notices_enabled", lambda: enabled)
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_plan_goal_compression_recovery", lambda *a, **k: (None, None))
    monkeypatch.setattr(server, "_is_successful_goal_turn", lambda *args: True)
    monkeypatch.setattr(goals, "count_active_delegations", lambda *a, **k: 0)
    monkeypatch.setattr(goals, "gather_background_processes", lambda *a, **k: [])
    manager = Mock()
    manager.evaluate_after_turn.return_value = {
        "message": "Goal judged", "should_continue": True, "continuation_prompt": "Continue work",
    }
    monkeypatch.setattr(server, "_active_goal_manager", lambda session: manager)
    session = {"session_key": "notice-policy", "agent": SimpleNamespace(session_id="notice-policy")}

    prompt = server._goal_followup_after_turn("view", session, {}, "complete", "Completed work")

    manager.evaluate_after_turn.assert_called_once()
    assert prompt == "Continue work"
    assert emitted == ([("status.update", "view", {"kind": "goal", "text": "Goal judged"})] if enabled else [])
