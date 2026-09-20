import pytest

from tools import async_delegation as ad
from tools.delegate_tool_child_run import _SchemaOutcome, _build_result_entry, _signal_child_stop
from tools.process_registry_notifications import format_process_notification


@pytest.fixture(autouse=True)
def reset_delegations():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def test_interrupt_callback_internal_type_error_is_not_retried():
    calls = []

    def interrupt(reason=None):
        calls.append(reason)
        raise TypeError("callback implementation failed")

    assert ad._call_interrupt(interrupt, "Interrupt failed: %s", reason="shutdown") is False
    assert calls == ["shutdown"]


@pytest.mark.parametrize("callback_kind", ["legacy", "positional_only", "varargs"])
def test_interrupt_callback_signature_compatibility(callback_kind):
    calls = []

    def legacy():
        calls.append(None)

    def positional_only(reason, /):
        calls.append(reason)

    def varargs(*args):
        calls.extend(args)

    callback = {"legacy": legacy, "positional_only": positional_only, "varargs": varargs}[callback_kind]
    assert ad._call_interrupt(callback, "Interrupt failed: %s", reason="shutdown") is True
    assert calls == ([None] if callback_kind == "legacy" else ["shutdown"])


class _Child:
    model = "test-model"
    _delegate_role = "leaf"
    session_prompt_tokens = 0
    session_completion_tokens = 0
    session_estimated_cost_usd = 0.0
    session_cost_status = "unknown"

    def __init__(self):
        self.received_reason = None

    def hard_interrupt(self, reason=None):
        self.received_reason = reason


def test_interrupt_reason_reaches_failure_entry_and_batch_completion_text():
    reason = "gateway shutdown (final-cleanup)"
    child = _Child()
    record = {
        "delegation_id": "deleg_x",
        "status": "running",
        "interrupt_fn": lambda reason: _signal_child_stop(child, reason),
    }
    with ad._records_lock:
        ad._records["deleg_x"] = record
    assert ad.interrupt_all(reason=reason) == 1
    entry = _build_result_entry(
        child,
        {"interrupted": True, "final_response": "", "api_calls": 18, "messages": []},
        task_index=0,
        duration=486.51,
        schema=_SchemaOutcome(None, None, [], 0),
    )

    assert child.received_reason == reason
    assert entry["interrupt_reason"] == reason
    text = format_process_notification(
        {
            "type": "async_delegation",
            "delegation_id": "deleg_x",
            "is_batch": True,
            "goals": ["long task"],
            "results": [entry],
            "status": "completed",
            "total_duration_seconds": 486.51,
        }
    )
    assert "status=interrupted" in text
    assert f"reason={reason}" in text
