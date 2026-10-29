"""Goal visibility must not stop CLI evaluation or continuation."""
import queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import cli


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_goal_notice_policy_preserves_continuation(monkeypatch, enabled):
    from hermes_cli import cli_loops_mixin, goals

    printed = []
    monkeypatch.setattr(cli, "_cprint", printed.append)
    monkeypatch.setattr(cli_loops_mixin, "goal_notices_enabled", lambda: enabled, raising=False)
    monkeypatch.setattr(goals, "count_active_delegations", lambda *a, **k: 0)
    monkeypatch.setattr(goals, "gather_background_processes", lambda *a, **k: [])
    manager = Mock()
    manager.is_active.return_value = True
    manager.evaluate_after_turn.return_value = {
        "message": "Goal judged", "should_continue": True, "continuation_prompt": "Continue work",
    }
    client = cli_loops_mixin.CLILoopsMixin()
    client._get_goal_manager = lambda: manager
    client._last_turn_started = True
    client._last_turn_result = {"final_response": "Completed work", "completed": True}
    client._pending_input = queue.Queue()
    client.session_id = "notice-policy"
    client.agent = SimpleNamespace(session_id="notice-policy")

    client._maybe_continue_goal_after_turn()

    manager.evaluate_after_turn.assert_called_once()
    assert client._pending_input.get_nowait() == "Continue work"
    assert printed == (["  Goal judged"] if enabled else [])

@pytest.mark.parametrize("notices", [False, True])
def test_human_turn_revival_keeps_snapshot_without_automatic_notice(monkeypatch, tmp_path, notices):
    from hermes_cli import goals
    from hermes_cli.cli_loops_mixin import CLILoopsMixin
    from hermes_constants import get_hermes_home
    get_hermes_home().joinpath("config.yaml").write_text(
        f"goals:\n  notices: {str(notices).lower()}\n")
    manager = goals.GoalManager("quiet-cli-revival")
    manager.set("finish the bounded work")
    with patch("hermes_cli.goals.judge_goal", return_value=(
        "blocked", "the irreversible constraint needs a human decision", False, None, False)):
        manager.evaluate_after_turn("Waiting for the decision")
    assert manager.state.status == "paused"
    spent = manager.state.turns_used
    client = CLILoopsMixin.__new__(CLILoopsMixin)
    client._get_goal_manager = lambda: manager
    with patch("cli._cprint") as output:
        client._revive_blocked_goal_for_user_turn("Decision received")
        client._revive_blocked_goal_for_user_turn("Decision received")
    assert output.call_count == int(notices)
    assert manager.is_active()
    assert manager.state.turns_used == spent
    assert goals.GoalManager(manager.session_id).is_active()
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "work remains", False, None, False)):
        assert manager.evaluate_after_turn("Decision received")["should_continue"]

@pytest.mark.parametrize("enabled", [False, True])
def test_cli_barrier_lift_keeps_wake_and_cleanup(monkeypatch, enabled):
    from hermes_cli import cli_loops_mixin
    manager = Mock()
    manager.is_parked.return_value = True
    manager.lifted_barrier_prompt.return_value = "Continue after wait"
    manager.state = SimpleNamespace(waiting_since=123, created_at="instance")
    client = cli_loops_mixin.CLILoopsMixin()
    client._get_goal_manager = lambda: manager
    client._pending_input = queue.Queue()
    monkeypatch.setattr(cli_loops_mixin, "goal_notices_enabled", lambda: enabled)
    with patch("cli._cprint") as output:
        client._maybe_resume_parked_goal()
    assert output.call_count == int(enabled)
    assert client._pending_input.get_nowait() == "Continue after wait"
    manager.mark_automatic_continuation_dispatched.assert_called_once_with(expected_instance="instance")
    manager.clear_lifted_wait.assert_called_once_with(123)