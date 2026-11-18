"""Overdue sends are held even when this process continuously owned the ticker."""
from datetime import timedelta

import pytest

from cron import executions, jobs, jobs_oneshot
from tests.cron.oneshot_helpers import NOW, store


@pytest.mark.parametrize("person_fields", [
    {},  # Agent prompts can perform sends even when normal delivery is local.
    {"no_agent": True, "deliver": "telegram:123"},
    {"no_agent": True, "send_freshness": {"target": "person"}},
    {"no_agent": True, "payload_path": "kept-message.txt"},
    {"no_agent": True, "payload_sha256": "a" * 64},
])
def test_overdue_person_send_held_before_scheduler_catch_up(store, person_fields):
    due = (NOW - timedelta(minutes=11)).isoformat()
    job = {
        "id": "person-send", "enabled": True, "state": "scheduled", "prompt": "Send the kept payload",
        "schedule": {"kind": "once", "run_at": due}, "next_run_at": due,
        "repeat": {"times": 1, "completed": 0}, "deliver": "local", **person_fields,
    }
    jobs.save_jobs([job])
    jobs_oneshot.record_due_scan(NOW - timedelta(minutes=12))
    assert jobs.get_due_jobs() == []
    held, = jobs.load_jobs()
    assert held["state"] == "missed" and held["enabled"] is False
    assert held["prompt"] == job["prompt"] and held["next_run_at"] == due
    row, = executions.list_executions(job_id=job["id"])
    assert row["status"] == "failed" and row["started_at"] is None
    assert jobs.get_due_jobs() == []
    assert executions.list_executions(job_id=job["id"]) == [row]
