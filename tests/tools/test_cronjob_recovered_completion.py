"""Recovery of a manual cron completion after its gateway waiter exits."""

from __future__ import annotations

import json

import pytest


@pytest.mark.parametrize("state, expected", [
    ("completed", "completed"), ("failed", "error"), ("unknown", "unknown"),
])
def test_dead_waiter_observes_exact_execution_once(monkeypatch, tmp_path, state, expected):
    from cron import executions
    from tools import async_delegation, cronjob_tools

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    monkeypatch.setattr(async_delegation, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(async_delegation, "_owner_liveness", lambda: lambda *_: False)
    monkeypatch.setattr(cronjob_tools, "get_job", lambda *_: {})
    execution = executions.create_execution("job-recovery", source="manual")
    record = {
        "delegation_id": "deleg_cron_recovery", "session_key": "agent:main:telegram:dm:1",
        "dispatched_at": 1234.0, "goal": "run", "role": "cron_run",
        "cron_execution_id": execution["id"], "cron_job_id": "job-recovery",
        "cron_job_name": "Recovery", "cron_deliver": "local",
    }
    async_delegation._persist_dispatch(record)
    # The waiter is gone but the worker is still live: do not announce unknown.
    assert async_delegation.recover_abandoned_delegations() == 0
    with async_delegation._transaction() as conn:
        persisted_state = conn.execute("SELECT state FROM async_delegations WHERE delegation_id=?",
                                       (record["delegation_id"],)).fetchone()[0]
    assert persisted_state == "running"

    if state == "unknown":
        monkeypatch.setattr(executions, "_PROCESS_ID", "new-gateway")
        monkeypatch.setattr(executions, "_owner_identity", lambda *_: "dead")
        assert executions.recover_interrupted_executions() == 1
    else:
        executions.finish_execution(execution["id"], success=state == "completed",
                                    error="failed before delivery" if state == "failed" else None,
                                    output="exact output")
    assert async_delegation.recover_abandoned_delegations() == 1
    assert async_delegation.recover_abandoned_delegations() == 0
    with async_delegation._transaction() as conn:
        row = conn.execute("SELECT state, event_json, delivery_state FROM async_delegations "
                           "WHERE delegation_id=?", (record["delegation_id"],)).fetchone()
    event = json.loads(row[1])
    assert row[0] == expected and row[2] == "pending"
    if state != "unknown":
        assert "exact output" in event["summary"]
    assert event["status"] == expected
    if state == "unknown":
        assert "cause is not known" in event["summary"]
    if state == "failed":
        assert "failed before delivery" in event["summary"]


def test_manual_completion_uses_exact_ledger_outcome(monkeypatch):
    from cron import executions
    from tools import cronjob_tools

    monkeypatch.setattr(executions, "get_execution", lambda _: {
        "status": "failed", "error": "worker failed", "output": "specific output",
    })
    monkeypatch.setattr(cronjob_tools, "get_job", lambda _: {})
    result = cronjob_tools._manual_run_completion(
        {"success": True}, "job", "name", "local", 0, execution_id="exact")
    assert result["status"] == "error"
    assert result["error"] == "worker failed"
    assert "specific output" in result["summary"]
