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


@pytest.mark.parametrize("age_hours, expected", [(23, "running"), (25, "unknown")])
def test_missing_cron_execution_recovery_is_bounded(monkeypatch, tmp_path, age_hours, expected):
    from cron import executions
    from tools import async_delegation as ad

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    monkeypatch.setattr(ad, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(ad, "_owner_liveness", lambda: lambda *_: False)
    now = 200000.0
    monkeypatch.setattr(ad.time, "time", lambda: now)
    record = {
        "delegation_id": f"missing-{age_hours}", "session_key": "agent:main:telegram:dm:1",
        "dispatched_at": now - age_hours * 3600, "goal": "run", "role": "cron_run",
        "cron_execution_id": "pruned-row", "cron_job_id": "job-recovery",
        "cron_job_name": "Recovery", "cron_deliver": "local",
    }
    ad._persist_dispatch(record)
    assert ad.recover_abandoned_delegations() == int(expected == "unknown")
    row = ad.get_durable_delegation(record["delegation_id"])
    assert row["state"] == expected
    if expected == "unknown":
        with ad._transaction() as conn:
            event_json = conn.execute("SELECT event_json FROM async_delegations WHERE delegation_id=?",
                                      (record["delegation_id"],)).fetchone()[0]
        assert "execution record missing" in json.loads(event_json)["error"].lower()
        assert ad.recover_abandoned_delegations() == 0


def test_dead_waiter_does_not_mutate_cron_execution_ledger(monkeypatch, tmp_path):
    from cron import executions
    from tools import async_delegation as ad

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    monkeypatch.setattr(ad, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(ad, "_owner_liveness", lambda: lambda *_: False)
    execution = executions.create_execution("job-recovery", source="manual")
    monkeypatch.setattr(executions, "_PROCESS_ID", "new-gateway")
    monkeypatch.setattr(executions, "_owner_identity", lambda *_: "dead")
    ad._persist_dispatch({
        "delegation_id": "dead-waiter", "session_key": "agent:main:telegram:dm:1",
        "dispatched_at": ad.time.time(), "goal": "run", "role": "cron_run",
        "cron_execution_id": execution["id"], "cron_job_id": "job-recovery",
        "cron_job_name": "Recovery", "cron_deliver": "local",
    })
    assert ad.recover_abandoned_delegations() == 0
    assert executions.get_execution(execution["id"])["status"] == "claimed"


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
