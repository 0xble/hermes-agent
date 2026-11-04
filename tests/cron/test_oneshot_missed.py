"""One-shot misses are accounted for, not confused with an in-process scan stall."""

import contextlib
import os
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from cron import delivery_queue, executions, incidents, jobs, scheduler
from cron import jobs_oneshot, scheduler_tick

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


@pytest.mark.parametrize("late,scan_age,same_pid,expected_due", [
    (660, 720, True, True),  # Last scan was before due, same ticker resumes 11 min late.
    (660, 1800, True, True),  # Job became due during the stall, first observable scan.
    (660, 600, True, True),  # Last scan was still inside grace.
    (660, 120, True, False),  # Already expired before this scheduler stall.
    (660, 720, False, False),  # Previous process's stamp proves no continuity.
    (660, 0, True, False),  # A previous scan already observed the expired window.
    (660, None, True, False),
    (0, 60, True, True),
    (60, 60, False, True),
])
def test_due_scan_accounts_for_miss_or_dispatches(store, monkeypatch, late, scan_age,
                                               same_pid, expected_due):
    due_at = (NOW - timedelta(seconds=late)).isoformat()
    job = {
        "id": "one-shot", "name": "one-shot", "prompt": "x", "enabled": True,
        "state": "scheduled", "schedule": {"kind": "once", "run_at": due_at},
        "next_run_at": due_at, "repeat": {"times": 1, "completed": 0}, "deliver": "local",
    }
    jobs.save_jobs([job])
    if scan_age is not None:
        pid = os.getpid() if same_pid else os.getpid() + 1
        (store / "ticker_due_scan").write_text(
            f"{NOW.timestamp() - scan_age} {pid} {jobs_oneshot._SCAN_PROCESS_ID}")

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

    deliver = Mock(return_value=(None, "not_configured"))
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
        # Main's durable outbox owns delivery; never bypass it with a direct send.
        deliver.assert_not_called()
        receipt = delivery_queue.get_status(f"oneshot-retired:{job['id']}")
        assert receipt is not None
        assert "not run; recreate to run" in receipt["content"]
        jobs.get_due_jobs(adapters=adapters, loop=loop)
        assert delivery_queue.get_status(receipt["execution_id"]) == receipt
        deliver.assert_not_called()


@pytest.mark.parametrize("marker", [
    "not-a-timestamp {pid} {nonce}",
    "nan {pid} {nonce}",
    "inf {pid} {nonce}",
    "{epoch} {pid}",  # Legacy evidence without process identity is not continuity.
    "{epoch} {pid} previous-process-with-recycled-pid",
])
def test_untrusted_scan_evidence_cannot_exempt_miss(store, marker):
    due_at = (NOW - timedelta(minutes=11)).isoformat()
    jobs.save_jobs([{
        "id": "invalid-evidence", "prompt": "x", "enabled": True, "state": "scheduled",
        "schedule": {"kind": "once", "run_at": due_at}, "next_run_at": due_at,
        "repeat": {"times": 1, "completed": 0}, "deliver": "local",
    }])
    (store / "ticker_due_scan").write_text(marker.format(
        pid=os.getpid(), nonce=jobs_oneshot._SCAN_PROCESS_ID, epoch=NOW.timestamp() - 720))
    # A genuinely fresh heartbeat remains irrelevant even for this same PID.
    jobs.record_ticker_heartbeat(success=True)
    assert jobs.get_due_jobs() == jobs.load_jobs() == []
    row, = executions.list_executions(job_id="invalid-evidence")
    assert row["status"] == "failed" and row["started_at"] is None


@pytest.mark.parametrize("multiplex", [True, False])
def test_provider_stall_after_profile_scan_preserves_catch_up(store, tmp_path, monkeypatch, multiplex):
    from cron import scheduler_provider
    from hermes_constants import get_hermes_home

    clock = [NOW]
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock[0])
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock[0])
    monkeypatch.setattr(jobs.time, "time", lambda: clock[0].timestamp())
    due_at = (NOW + timedelta(seconds=30)).isoformat()
    jobs.save_jobs([{
        "id": "never-started", "prompt": "x", "enabled": True, "state": "scheduled",
        "schedule": {"kind": "once", "run_at": due_at}, "next_run_at": due_at,
        "repeat": {"times": 1, "completed": 0}, "deliver": "local",
    }])
    other = tmp_path / "other"
    other.mkdir()
    stop = threading.Event()
    scans = []
    real_tick = scheduler.tick

    def tick(**kwargs):
        home = get_hermes_home()
        scans.append(home)
        real_tick(**{**kwargs, "sync": True})
        # Single profile: stall after its scan. Multiplex: A scanned before due,
        # then B stalls the cycle; the real provider subsequently refreshes A's heartbeat.
        if (home == other if multiplex else home == tmp_path):
            clock[0] = NOW + timedelta(minutes=11)

    waits = []

    def wait(*args, **kwargs):
        waits.append(True)
        if len(waits) == 2:
            stop.set()

    def run(job, **kwargs):
        executions.finish_execution(job["execution_id"], success=True)
        jobs.mark_job_run(job["id"], success=True)
        return True

    run_one = Mock(side_effect=run)
    monkeypatch.setattr(scheduler, "tick", tick)
    monkeypatch.setattr(scheduler, "run_one_job", run_one)
    monkeypatch.setattr(scheduler_provider, "wait_for_next_tick", wait)
    provider = scheduler_provider.InProcessCronScheduler()
    provider.start(stop, profile_homes=[("a", tmp_path), ("b", other)] if multiplex else None)

    expected_scans = [tmp_path, other, tmp_path, other] if multiplex else [tmp_path, tmp_path]
    assert scans == expected_scans
    row, = executions.list_executions(job_id="never-started")
    assert row["status"] == "completed"
    assert row["scheduled_instant"] == due_at
    run_one.assert_called_once()


@pytest.mark.parametrize("failure_boundary", ["terminalize", "publish"])
def test_missed_accounting_failure_never_leaves_claim_or_duplicate(store, monkeypatch, failure_boundary):
    due_at = (NOW - timedelta(minutes=11)).isoformat()
    jobs.save_jobs([{
        "id": "atomic-miss", "prompt": "x", "enabled": True, "state": "scheduled",
        "schedule": {"kind": "once", "run_at": due_at}, "next_run_at": due_at,
        "repeat": {"times": 1, "completed": 0}, "deliver": "local",
    }])
    # A failed terminalization must roll back insertion too. A failed projection
    # after commit may retain the terminal row, but retry must reuse that same row.
    attr = "_prune_unlocked" if failure_boundary == "terminalize" else "_emit_execution_state"
    original = getattr(executions, attr)
    monkeypatch.setattr(executions, attr, Mock(side_effect=OSError("accounting unavailable")))
    assert jobs.get_due_jobs() == []
    assert [j["id"] for j in jobs.load_jobs()] == ["atomic-miss"]
    rows = executions.list_executions(job_id="atomic-miss")
    assert all(row["status"] == "failed" and row["started_at"] is None for row in rows)
    if failure_boundary == "terminalize":
        assert rows == []
    monkeypatch.setattr(executions, attr, original)
    assert jobs.get_due_jobs() == jobs.load_jobs() == []
    row, = executions.list_executions(job_id="atomic-miss")
    assert row["status"] == "failed" and row["scheduled_instant"] == due_at


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
    receipt = delivery_queue.get_status(f"oneshot-retired:{job['id']}")
    assert receipt is not None and receipt["status"] == "pending"
    deliver.assert_not_called()
