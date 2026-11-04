"""A previous arm's history must never spend a new one-shot occurrence."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from cron import delivery_queue, executions, jobs
from hermes_cli.cron import cron_resume
from tests.cron.oneshot_helpers import NOW, store


@pytest.mark.parametrize("arm,started", [
    ("fresh", False),
    ("fresh-queued", False),
    ("fresh-previous-started", False),
    ("fresh-missed", False),
    ("rearmed-at", False),
    ("rearmed-run-now", False),
    ("started-crashed", True),
    ("started-ledger", True),
    ("completed", True),
])
@pytest.mark.parametrize("lateness", [60, 660])
@pytest.mark.parametrize("missing_slot", [False, True])
def test_overdue_occurrence_is_held_unless_this_arm_started(store, monkeypatch, arm, started, lateness, missing_slot):
    due = NOW - timedelta(seconds=lateness)
    old = NOW - timedelta(days=2)
    job = {
        "id": "rearmed", "name": "rearmed", "prompt": "keep this payload", "deliver": "local",
        "enabled": True, "state": "scheduled", "schedule": {"kind": "once", "run_at": due.isoformat()},
        "next_run_at": due.isoformat(), "repeat": {"times": 1, "completed": 0},
    }
    if not arm.startswith("fresh"):
        job.update(state="completed", enabled=False, last_run_at=old.isoformat(), last_status="success",
                   schedule={"kind": "once", "run_at": old.isoformat()}, next_run_at=None,
                   repeat={"times": 1, "completed": 1})
    jobs.save_jobs([job])
    if not arm.startswith("fresh"):
        # Real CLI re-arm resets the dispatch budget but intentionally retains run history.
        monkeypatch.setattr(jobs, "_hermes_now", lambda: due)
        assert cron_resume(SimpleNamespace(job_id=job["id"],
                           run_at=None if arm == "rearmed-run-now" else due.isoformat(),
                           run_now=arm == "rearmed-run-now")) == 0
        monkeypatch.setattr(jobs, "_hermes_now", lambda: NOW)
        assert jobs.get_job(job["id"])["last_run_at"] == old.isoformat()
    if arm == "fresh-queued":
        executions.create_execution(job["id"], source="scheduler", scheduled_instant=due.isoformat())
    elif arm == "fresh-previous-started":
        previous = executions.create_execution(job["id"], source="scheduler", scheduled_instant=old.isoformat())
        executions.mark_execution_running(previous["id"])
    elif arm == "fresh-missed":
        executions.record_missed_execution(job["id"], scheduled_instant=due.isoformat(), error="missed")
    if started:
        if arm != "started-ledger":
            assert jobs.claim_dispatch(job["id"]) is True
        row = executions.create_execution(job["id"], source="scheduler", scheduled_instant=due.isoformat())
        executions.mark_execution_running(row["id"])
        executions.finish_execution(row["id"], success=arm == "completed", error="crashed" if arm != "completed" else None)
    if arm == "completed":
        jobs.mark_job_run(job["id"], success=True)

    if missing_slot and arm != "completed":
        records = jobs.load_jobs()
        records[0]["next_run_at"] = None
        jobs.save_jobs(records)
    due_jobs = jobs.get_due_jobs()
    if lateness == 60 and not started:
        assert [j["id"] for j in due_jobs] == [job["id"]]
        runnable = jobs.get_job(job["id"])
        assert runnable is not None and runnable["state"] == "scheduled"
        assert runnable["prompt"] == job["prompt"]
        return
    assert due_jobs == []
    remaining = jobs.get_job(job["id"])
    if arm == "completed":
        assert remaining is not None and remaining["state"] == "completed"
    elif started:
        assert remaining is None
        receipt = delivery_queue.get_status(f"oneshot-retired:{job['id']}")
        assert receipt is not None and "partial effects" in receipt["content"]
        assert "interrupted" in receipt["content"]
    else:
        assert remaining is not None, "never-started re-arm lost its payload"
        assert remaining["state"] == "missed" and remaining["enabled"] is False
        assert remaining["prompt"] == job["prompt"] and remaining["next_run_at"] == due.isoformat()
        receipt = delivery_queue.get_status(f"oneshot-missed:{job['id']}:{due.isoformat()}")
        assert receipt is not None and "Payload kept" in receipt["content"]
        assert json.loads(receipt["job_json"])["deliver"] == "origin"
        assert jobs.get_due_jobs() == []
        assert jobs.get_job(job["id"])["prompt"] == job["prompt"]


@pytest.mark.parametrize("entry", ["claim", "scan", "recover-missing-slot"])
def test_rearmed_old_history_does_not_hide_interrupted_dispatch(store, monkeypatch, entry):
    due = NOW - timedelta(seconds=60)
    old = NOW - timedelta(days=2)
    jobs.save_jobs([{
        "id": "spent", "prompt": "payload", "deliver": "local", "enabled": True, "state": "scheduled",
        "schedule": {"kind": "once", "run_at": due.isoformat()}, "next_run_at": due.isoformat(),
        "repeat": {"times": 1, "completed": 0 if entry == "recover-missing-slot" else 1},
        "last_run_at": old.isoformat(), "last_status": "success",
    }])
    if entry == "recover-missing-slot":
        records = jobs.load_jobs()
        records[0]["next_run_at"] = None
        jobs.save_jobs(records)
        due_jobs = jobs.get_due_jobs()
        assert [job["id"] for job in due_jobs] == ["spent"]
    else:
        assert (jobs.claim_dispatch("spent") if entry == "claim" else jobs.get_due_jobs()) in (False, [])
        assert jobs.get_job("spent") is None
        receipt = delivery_queue.get_status("oneshot-retired:spent")
        assert receipt is not None and "partial effects" in receipt["content"]
