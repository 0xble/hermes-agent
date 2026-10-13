"""TUI/Desktop successful empty turns remain goal evaluation boundaries."""

import pytest

from tui_gateway.prompt_turn import _is_successful_goal_turn


@pytest.mark.parametrize("text", ["", "   ", "[SILENT]", "failure notice"])
@pytest.mark.parametrize("outcome", ["success", "failed", "interrupted", "incomplete", "handoff"])
def test_goal_admission_uses_completion_not_reply_length(text, outcome):
    result = {"final_response": text}
    if outcome == "success":
        result["completed"] = True
    elif outcome == "failed":
        result["failed"] = True
    elif outcome == "interrupted":
        result["interrupted"] = True
    else:
        result["completed"] = False
        if outcome == "handoff":
            result["turn_exit_reason"] = "max_iterations_reached(3/3)"
    admitted = outcome == "success" or (outcome == "handoff" and bool(text.strip()))
    assert _is_successful_goal_turn(result, "complete", text) is admitted
    assert _is_successful_goal_turn(result, "error", text) is False
    assert _is_successful_goal_turn(result, "interrupted", text) is False
