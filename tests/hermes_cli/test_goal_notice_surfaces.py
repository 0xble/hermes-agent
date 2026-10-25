"""Goal visibility must not stop CLI evaluation or continuation."""
import queue
from types import SimpleNamespace
from unittest.mock import Mock

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
    client._last_assistant_response_text = lambda: "Completed work"
    client._pending_input = queue.Queue()
    client.session_id = "notice-policy"
    client.agent = SimpleNamespace(session_id="notice-policy")

    client._maybe_continue_goal_after_turn()

    manager.evaluate_after_turn.assert_called_once()
    assert client._pending_input.get_nowait() == "Continue work"
    assert printed == (["  Goal judged"] if enabled else [])
