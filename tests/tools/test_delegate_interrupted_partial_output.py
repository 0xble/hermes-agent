"""An interrupted child reports real partial output, not a synthetic transcript closer.

The legacy closing row ``Operation interrupted.`` remains readable; newer rows
use an internal marker. Neither is a child reply. See #114456.
"""
from types import SimpleNamespace

import pytest

from tools.delegate_tool_child_run import _SchemaOutcome, _build_result_entry

@pytest.mark.parametrize("placeholder", [
    "Operation interrupted.",
    "[No reply: this turn was interrupted before completion. Do not repeat this internal marker.]",
])
def test_interrupted_child_entry_carries_its_partial_output(placeholder):
    messages = [
        {"role": "user", "content": "kickoff"},
        {"role": "assistant", "content": [{"type": "text", "text": "Audited 3 of 7 modules; two findings so far."}],
         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "[Command interrupted]"},
        {"role": "assistant", "content": placeholder},
    ]
    result = {"final_response": placeholder, "messages": messages, "api_calls": 2,
              "completed": False, "interrupted": True}
    child = SimpleNamespace(model="m", session_estimated_cost_usd=0.0, session_cost_status="unknown",
                            session_prompt_tokens=1, session_completion_tokens=1, _delegate_role="leaf")

    entry = _build_result_entry(child, result, 0, 12.5, _SchemaOutcome(None, None, [], 0))

    assert entry["status"] == entry["exit_reason"] == "interrupted"
    assert entry["summary"] == "Audited 3 of 7 modules; two findings so far."
    if placeholder == "Operation interrupted.":
        assert entry["error"] == placeholder
    else:
        assert "error" not in entry


def test_interrupted_child_with_only_internal_marker_has_no_output():
    marker = "[No reply: this turn was interrupted before completion. Do not repeat this internal marker.]"
    messages = [{"role": "tool", "content": "ok"}, {"role": "assistant", "content": marker}]
    result = {"final_response": "Operation interrupted.", "messages": messages, "interrupted": True}
    child = SimpleNamespace(model="m", session_estimated_cost_usd=0.0, session_cost_status="unknown",
                            session_prompt_tokens=0, session_completion_tokens=0, _delegate_role="leaf")
    entry = _build_result_entry(child, result, 0, 1.0, _SchemaOutcome(None, None, [], 0))
    assert entry["status"] == "interrupted"
    assert entry["summary"] == "Operation interrupted."
    assert marker not in entry["summary"]

    result["final_response"] = marker
    entry = _build_result_entry(child, result, 0, 1.0, _SchemaOutcome(None, None, [], 0))
    assert entry["status"] == "interrupted"
    assert entry["summary"] == ""
