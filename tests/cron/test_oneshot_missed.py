"""One-shot misses are accounted for, not confused with an in-process scan stall."""

import contextlib
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from cron import executions, incidents, jobs, scheduler
from cron import scheduler_tick

NOW = datetime(2026, 10, 29, 17, 21, 28, tzinfo=UTC)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "_hermes_now", lambda: NOW)
    monkeypatch.setattr(executions, "_hermes_now", lambda: NOW)
    monkeypatch.setattr(scheduler, "_should_yield_tick_to_fresh_gateway", lambda: None)
    monkeypatch.setattr(scheduler_tick, "_pre_dispatch_housekeeping", lambda *args: None)
    monkeypatch.setattr(scheduler, "_maybe_run_worktree_maintenance", lambda: None)
    monkeypatch.setattr(scheduler, "_sweep_mcp_orphans", lambda: None)
    jobs.ensure_dirs()
    return tmp_path / "cron"


@pytest.mark.parametrize("late,heartbeat_age,same_pid,expected_due", [
    (660, 720, True, True),  # Last scan was before due, same ticker resumes 11 min late.
    (660, 1800, True, True),  # Job became due during the stall, first observable scan.
    (660, 600, True, True),  # Last scan was still inside grace.
    (660, 120, True, False),  # Already expired before this scheduler stall.
    (660, 720, False, False),  # Previous process's stamp proves no continuity.
    (660, 0, True, False),  # New process's startup heartbeat is after the deadline.
    (660, None, True, False),
    (0, 60, True, True),
    (60, 60, False, True),
])
def test_due_scan_accounts_for_miss_or_dispatches(store, monkeypatch, late, heartbeat_age,
                                               same_pid, expected_due):
    due_at = (NOW - timedelta(seconds=late)).isoformat()
    job = {
        "id": "one-shot", "name": "one-shot", "prompt": "x", "enabled": True,
        "state": "scheduled", "schedule": {"kind": "once", "run_at": due_at},
        "next_run_at": due_at, "repeat": {"times": 1, "completed": 0}, "deliver": "local",
    }
    jobs.save_jobs([job])
    if heartbeat_age is not None:
        pid = os.getpid() if same_pid else os.getpid() + 1
        (store / "ticker_heartbeat").write_text(f"{NOW.timestamp() - heartbeat_age} {pid}")

    locked = False
    original_lock = jobs._jobs_lock

    @contextlib.contextmanager
    def track_lock():
        nonlocal locked
        with original_lock():
            locked = True
            try:
                yield
            finally:
                locked = False

    monkeypatch.setattr(jobs, "_jobs_lock", track_lock)
    adapters, loop = object(), object()

    delivery_state = []

    def delivered(job, error, **kwargs):
        # Delivery errors are contained by production, so observe here and assert afterward.
        delivery_state.append((locked, jobs.load_jobs(), bool(incidents.list_incidents()), kwargs))
        return None, "not_configured"

    deliver = Mock(side_effect=delivered)
    monkeypatch.setattr(scheduler, "_deliver_crash_failure", deliver)

    def run(job, **kwargs):
        executions.finish_execution(job["execution_id"], success=True)
        jobs.mark_job_run(job["id"], success=True)
        return True

    run_one = Mock(side_effect=run)
    monkeypatch.setattr(scheduler, "run_one_job", run_one)
    ran = scheduler.tick(verbose=False, sync=True, adapters=adapters, loop=loop)
    if expected_due:
        assert ran == 1
        run_one.assert_called_once()
        row, = executions.list_executions(job_id=job["id"])
        assert row["status"] == "completed"
        deliver.assert_not_called()
    else:
        assert ran == 0 and jobs.load_jobs() == []
        run_one.assert_not_called()
        row, = executions.list_executions(job_id=job["id"])
        assert row["status"] == "failed" and row["started_at"] is None
        assert row["scheduled_instant"] == due_at
        assert "missed" in row["error"].lower()
        assert due_at in row["error"] and NOW.isoformat() in row["error"]
        assert incidents.list_incidents()[0]["job_id"] == job["id"]
        deliver.assert_called_once()
        assert delivery_state == [(False, [], True, {"adapters": adapters, "loop": loop})]
        jobs.get_due_jobs(adapters=adapters, loop=loop)
        deliver.assert_called_once()


@pytest.mark.parametrize("failed_store", ["incident", "jobs"])
def test_retirement_requires_durable_accounting_and_save(store, monkeypatch, failed_store):
    due_at = (NOW - timedelta(minutes=11)).isoformat()
    job = {
        "id": "missed", "name": "missed", "prompt": "x", "enabled": True,
        "state": "scheduled", "schedule": {"kind": "once", "run_at": due_at},
        "next_run_at": due_at, "repeat": {"times": 1, "completed": 0}, "deliver": "local",
    }
    jobs.save_jobs([job])
    deliver = Mock(return_value=(None, "not_configured"))
    monkeypatch.setattr(scheduler, "_deliver_crash_failure", deliver)
    target, attr = (incidents, "upsert_incident") if failed_store == "incident" else (jobs, "save_jobs")
    original = getattr(target, attr)
    monkeypatch.setattr(target, attr, Mock(side_effect=OSError("store unavailable")))
    assert jobs.get_due_jobs() == []
    assert [j["id"] for j in jobs.load_jobs()] == [job["id"]]
    deliver.assert_not_called()
    monkeypatch.setattr(target, attr, original)
    assert jobs.get_due_jobs() == jobs.load_jobs() == []
    assert len(executions.list_executions(job_id=job["id"])) == 1
    assert incidents.list_incidents()
    deliver.assert_called_once()
