"""An intentional review handoff is a phase boundary, not an omitted disposition.

Regression: a dispatched native review ends the turn with no assistant answer, so
the bounded disposition correction had no completed boundary to correct. It raised,
then appended a user-facing "still need an explicit disposition" warning to an
otherwise silent internal handoff. Retained results must stay visible and be
reconciled when the parent actually answers.
"""
import json
from types import SimpleNamespace

import pytest

from agent.delegation_disposition import finish_result_turn
from gateway.run import _normalize_empty_agent_response

WARNING = "Some delegated results still need an explicit disposition"


def _agent(missing, *, tracking_error=False):
    calls = []

    def progress(event, **kwargs):
        calls.append(event)
        return {"missing": json.loads(json.dumps(missing))}

    agent = SimpleNamespace(
        api_mode="chat_completions", provider="openai-compat", client=None,
        session_id="review-handoff", max_iterations=8, _interrupt_requested=False,
        _delegation_result_turn="result-turn",
        _delegation_result_tracking_error=tracking_error,
        tool_progress_callback=progress, _emit_warning=lambda text: None,
    )
    return agent, calls


def _handoff(**overrides):
    """Shape of a finalized turn whose tool round dispatched a native review."""
    return {"final_response": "", "messages": [
        {"role": "user", "content": "review"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "r"}]},
        {"role": "tool", "tool_call_id": "r", "content": '{"status":"running"}'},
    ], "api_calls": 1, "completed": True, "interrupted": False, "failed": False,
        "turn_exit_reason": "review_dispatched", **overrides}


def _run(agent, result):
    def run_turn(*args, **kwargs):
        raise AssertionError("A review handoff must not trigger a correction turn")

    return finish_result_turn(agent, result, run_turn, None, "parent")


def test_review_handoff_suppresses_correction_warning():
    missing = [{"parent_task_id": "parent", "thread_ref": "A", "attempt": 0}]
    agent, _ = _agent(missing)

    result = _run(agent, _handoff())

    assert result["final_response"] == ""
    assert not result.get("delegation_disposition_unresolved")
    assert _normalize_empty_agent_response(result, result["final_response"]) == ""
    # Durable retention and later authority are exercised with real ledgers in
    # tests/gateway/test_review_handoff_dispositions.py.


def test_review_handoff_does_not_mask_a_tracking_failure():
    agent, _ = _agent([], tracking_error=True)

    result = _run(agent, _handoff())

    assert result["delegation_disposition_unresolved"] is True
    assert WARNING in result["final_response"]


@pytest.mark.parametrize("exit_reason", ["unknown", "budget_exhausted", None])
def test_ordinary_answer_turn_still_requires_disposition(exit_reason):
    """The guard is scoped to the review boundary, not to every empty answer."""
    agent, calls = _agent([{"parent_task_id": "parent", "thread_ref": "A", "attempt": 0}])
    corrections = []

    def run_turn(agent_, prompt, system_message, history, task_id, **kwargs):
        corrections.append(prompt)
        return {"messages": history, "api_calls": 1}

    result = finish_result_turn(
        agent, _handoff(final_response="Answer", turn_exit_reason=exit_reason),
        run_turn, None, "parent")

    assert len(corrections) == 1
    assert result["delegation_disposition_unresolved"] is True
    assert result["final_response"].startswith("Answer")
    assert WARNING in result["final_response"]
    assert agent.max_iterations == 8
    assert calls.count("subagent.result_turn") >= 2
