"""Model notifications report unfinished outcomes, not routine lifecycle causes."""
import pytest

from tools.delegation_resume import build_recovery_instruction
from tools.process_registry_notifications import format_process_notification


@pytest.mark.parametrize("source", [None, "kill_all", "gateway_shutdown"])
def test_goal_lifted_barrier_is_outcome_only(monkeypatch, source):
    from hermes_cli import goals

    outcome = None if source is None else {
        "completion_reason": "killed", "termination_source": source, "exit_code": -15,
    }
    monkeypatch.setattr(goals, "_process_outcome", lambda sid: outcome)
    text = goals._barrier_lift_note(goals.GoalState(goal="verify", waiting_on_session="proc_test"))
    for cause in ("gateway_shutdown", "shutdown", "restart", "gateway"):
        assert cause not in text.lower()
    if source is None:
        assert "no longer tracked" in text and "outcome is unknown" in text
        assert "output or artifacts" in text and "before rerunning" in text
    else:
        assert "stopped before it finished (exit -15)" in text
        assert "verify the real state" in text


def test_goal_explicit_user_kill_keeps_its_attribution(monkeypatch):
    from hermes_cli import goals

    monkeypatch.setattr(goals, "_process_outcome", lambda sid: {
        "completion_reason": "killed", "termination_source": "process.kill", "exit_code": -15,
    })
    text = goals._barrier_lift_note(goals.GoalState(goal="verify", waiting_on_session="proc_test"))
    assert "was killed by an explicit kill before it finished (exit -15)" in text


@pytest.mark.parametrize("exit_code", [-15, 143, -9])
def test_routine_process_stop_is_outcome_only(exit_code):
    text = format_process_notification({
        "session_id": "proc_test", "command": "verify", "output": "partial proof",
        "completion_reason": "killed", "termination_source": "gateway_shutdown",
        "exit_code": exit_code,
    })
    assert text is not None
    for cause in ("gateway_shutdown", "shutdown", "restart", "gateway", "SIGTERM"):
        assert cause not in text
    assert f"stopped before it finished (exit code {exit_code})." in text
    assert "If its result is still needed, rerun it" in text
    assert "reconcile any non-idempotent effect first" in text
    assert "partial proof" in text


@pytest.mark.parametrize("reason,source,status", [
    ("killed", "process.kill", "terminated by process.kill"),
    ("killed", "kill_all", "terminated by kill_all"),
    ("lost", "backend_lost", "marked lost because the process backend disappeared"),
    ("failed_start", "failed_start", "failed to start"),
])
def test_other_process_outcomes_keep_their_wording(reason, source, status):
    text = format_process_notification({
        "session_id": "proc_test", "completion_reason": reason,
        "termination_source": source, "exit_code": -15,
    })
    assert text is not None
    assert status in text
    assert "exit code -15, SIGTERM" in text
    assert "reconcile any non-idempotent effect first" not in text


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("reason", [
    "gateway shutdown (final-cleanup)", "Gateway restarting", "Gateway shutting down",
    "gateway shutdown", "gateway_shutdown", "shutdown",
])
def test_delegation_stop_notice_is_outcome_only(batch, reason):
    entry = {"status": "interrupted", "error": reason, "interrupt_reason": reason}
    evt = {"type": "async_delegation", "delegation_id": "deleg_test", **entry}
    if batch:
        evt.update(is_batch=True, results=[entry], goals=["verify"])
    text = format_process_notification(evt)
    assert text is not None
    for cause in ("shutdown", "restart", "gateway"):
        assert cause not in text.lower()
    assert "stopped before finishing" in text
    assert entry["interrupt_reason"] == reason
    assert entry["error"] == reason


@pytest.mark.parametrize("reason", ["stop_command", "session_end", "user requested a gateway restart"])
def test_other_delegation_stop_reasons_are_preserved(reason):
    text = format_process_notification({
        "type": "async_delegation", "delegation_id": "deleg_test",
        "status": "interrupted", "interrupt_reason": reason, "error": reason,
    })
    assert text is not None
    assert f"The subagent was interrupted before completing: {reason}" in text


@pytest.mark.parametrize("state", ["interrupted", "unknown"])
def test_delegation_recovery_instruction_is_outcome_only(state):
    text = build_recovery_instruction({"state": state, "delegation_id": "deleg_test"})
    assert text is not None
    for cause in ("shutdown", "restart", "gateway", "owning process exited"):
        assert cause not in text.lower()
    assert "verify current state" in text
    assert "never repeat an external write" in text
