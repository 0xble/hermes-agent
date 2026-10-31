"""Goal visibility must not stop TUI evaluation or continuation."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

@pytest.mark.parametrize("notices", [False, True])
def test_tui_human_revival_keeps_snapshot_and_progress(monkeypatch, notices):
    from hermes_cli import goals
    from hermes_constants import get_hermes_home
    from tui_gateway import server
    get_hermes_home().joinpath("config.yaml").write_text(
        f"goals:\n  notices: {str(notices).lower()}\n")
    manager = goals.GoalManager("quiet-tui-revival")
    manager.set("finish the bounded work")
    with patch("hermes_cli.goals.judge_goal", return_value=(
        "blocked", "the irreversible constraint needs a human decision", False, None, False)):
        manager.evaluate_after_turn("Waiting for a decision")
    assert manager.state.status == "paused"
    spent = manager.state.turns_used
    with patch("tui_gateway.server._emit") as emit, patch(
            "tui_gateway.server._publish_session_control_snapshot") as snapshot:
        session = {"session_key": manager.session_id}
        server._revive_blocked_goal_for_user_turn("room", session, True)
        server._revive_blocked_goal_for_user_turn("room", session, True)
    snapshot.assert_called_once_with("room", session, only_if_present=True)
    manager = goals.GoalManager(manager.session_id)
    assert emit.call_count == int(notices)
    assert manager.is_active()
    assert manager.state.turns_used == spent
    assert goals.GoalManager(manager.session_id).is_active()
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "work remains", False, None, False)):
        assert manager.evaluate_after_turn("Decision received")["should_continue"]


@pytest.mark.parametrize("notices,shown", [("true", True), ("false", False), ("hidden", False)])
def test_tui_loop_cards_follow_loop_notices(monkeypatch, notices, shown):
    from hermes_constants import get_hermes_home
    from tui_gateway import server
    get_hermes_home().joinpath("config.yaml").write_text(f"loops:\n  notices: {notices}\n")
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    server._notif_loop_card("room", "✓ Loop finished after 3 ticks — task complete.")
    assert bool(emitted) is shown
