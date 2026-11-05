"""A slow execution ledger must not block jobs.json writers or certify stale snapshots."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import os
import subprocess
import sys
import threading

import pytest

from cron import executions, jobs, occurrences


@pytest.fixture
def store(tmp_path, monkeypatch):
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    job = {"id": "due", "name": "due", "prompt": "test", "enabled": True,
           "state": "scheduled", "schedule": {"kind": "interval", "minutes": 60},
           "next_run_at": now.isoformat(), "repeat": {"times": None, "completed": 0}}
    jobs.save_jobs([job])
    return job, now


def _run_thread(fn):
    result, errors = [], []

    def run():
        try:
            result.append(fn())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result, errors


@pytest.mark.parametrize("operation", ["scan", "claim"])
def test_blocked_ledger_leaves_cross_process_jobs_lock_available(store, monkeypatch, operation):
    entered, release = threading.Event(), threading.Event()
    real_transaction = executions._transaction

    @contextmanager
    def blocked():
        entered.set()
        assert release.wait(15), "test did not release the ledger"
        with real_transaction() as conn:
            yield conn

    monkeypatch.setattr(executions, "_transaction", blocked)
    fn = jobs.get_due_jobs if operation == "scan" else lambda: jobs.claim_job_for_fire("due")
    thread, result, errors = _run_thread(fn)
    try:
        assert entered.wait(10), "operation did not reach ledger"
        code = """
import sys
from pathlib import Path
from cron import jobs
jobs.CRON_DIR = Path(sys.argv[1])
jobs.JOBS_FILE = jobs.CRON_DIR / 'jobs.json'
jobs.OUTPUT_DIR = jobs.CRON_DIR / 'output'
jobs._JOBS_LOCK_TIMEOUT_SECONDS = 1
with jobs._jobs_lock():
    jobs.pause_job('due')
print('paused')
"""
        child = subprocess.run([sys.executable, "-c", code, str(jobs.CRON_DIR)],
                               env=dict(os.environ), capture_output=True, text=True,
                               timeout=10, check=False)
        assert child.returncode == 0, child.stdout + child.stderr
        assert "Timed out" not in child.stderr, child.stderr
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive() and not errors, errors
    assert jobs.load_jobs()[0]["enabled"] is False
    assert result == ([[]] if operation == "scan" else [False])


@pytest.mark.parametrize("operation", ["scan", "claim"])
def test_ledger_and_liveness_reads_are_outside_jobs_lock(store, monkeypatch, operation):
    job, now = store
    job["fire_claim"] = {"at": now.isoformat(), "by": "dead:123"}
    job["next_run_at"] = (now + timedelta(hours=4)).isoformat()
    jobs.save_jobs([job])
    depths = []
    real_completed = occurrences.completed_occurrence

    def completed(*args):
        depths.append(getattr(jobs._jobs_lock_state, "depth", 0))
        return real_completed(*args)

    def dead(_claim):
        depths.append(getattr(jobs._jobs_lock_state, "depth", 0))
        return True

    monkeypatch.setattr(occurrences, "completed_occurrence", completed)
    monkeypatch.setattr(jobs, "_claim_owner_is_dead", dead)
    (jobs.get_due_jobs() if operation == "scan" else jobs.claim_job_for_fire("due"))
    assert depths and all(depth == 0 for depth in depths), depths


def test_unrelated_edit_does_not_starve_due_job(store, monkeypatch):
    job, now = store
    sibling = dict(job, id="sibling", name="sibling", next_run_at=(now + timedelta(hours=1)).isoformat())
    jobs.save_jobs([job, sibling])
    real = occurrences.completed_occurrence

    def completed(record, instant):
        if record["id"] == "due":
            jobs.update_job("sibling", {"prompt": "concurrent edit"})
        return real(record, instant)

    monkeypatch.setattr(occurrences, "completed_occurrence", completed)
    assert [row["id"] for row in jobs.get_due_jobs()] == ["due"]
    edited = jobs.get_job("sibling")
    assert edited is not None and edited["prompt"] == "concurrent edit"


def test_completion_and_same_slot_fire_claim_are_preserved(store):
    job, now = store
    row = executions.create_execution("due", source="test", scheduled_instant=now.isoformat())
    executions.finish_execution(row["id"], success=True)
    assert jobs.get_due_jobs() == []
    jobs.save_jobs([job])  # an independent rollback of jobs.json cannot undo completion
    assert jobs.claim_job_for_fire("due") is False

    later = dict(job, next_run_at=(now - timedelta(minutes=1)).isoformat())
    jobs.save_jobs([later])
    assert jobs.claim_job_for_fire("due") is True
    assert jobs.claim_job_for_fire("due") is False


def test_two_concurrent_scans_only_stamp_one_oneshot_claim(store, monkeypatch):
    job, now = store
    job.update(schedule={"kind": "once", "run_at": now.isoformat()},
               repeat={"times": 1, "completed": 0})
    jobs.save_jobs([job])
    barrier = threading.Barrier(2)
    real = occurrences.completed_occurrence

    def completed(*args):
        result = real(*args)
        if not getattr(jobs._jobs_lock_state, "depth", 0):
            barrier.wait(10)
        return result

    monkeypatch.setattr(occurrences, "completed_occurrence", completed)
    first, a, ae = _run_thread(jobs.get_due_jobs)
    second, b, be = _run_thread(jobs.get_due_jobs)
    first.join(15)
    second.join(15)
    assert not first.is_alive() and not second.is_alive()
    assert not ae and not be, ae + be
    assert sorted(len(rows) for rows in a + b) == [0, 1]


def test_concurrent_schedule_edit_is_evaluated_from_current_locked_record(store, monkeypatch):
    _job, now = store
    future = (now + timedelta(hours=4)).isoformat()
    real = occurrences.completed_occurrence

    def completed(*args):
        jobs.update_job("due", {"next_run_at": future})
        return real(*args)

    monkeypatch.setattr(occurrences, "completed_occurrence", completed)
    assert jobs.get_due_jobs() == []
    assert jobs.load_jobs()[0]["next_run_at"] == future


@pytest.mark.parametrize("scenario", ["retired", "missed_window", "outage"])
def test_prefetch_preserves_base_notice_save_and_recovery_order(store, monkeypatch, scenario):
    """Differential: the base locked gate without preflight vs the public prefetched gate."""
    from cron import owner_alerts

    job, now = store
    job["next_run_at"] = (now - timedelta(hours=1)).isoformat()
    if scenario != "missed_window":
        job["schedule"] = {"kind": "once", "run_at": job["next_run_at"]}
        job["repeat"] = {"times": 1, "completed": 0}
    else:
        job["max_catch_up_lateness_seconds"] = 0
    events = []
    real_save = jobs.save_jobs

    def event(name):
        events.append((name, getattr(jobs._jobs_lock_state, "depth", 0)))

    def save(*args, **kwargs):
        event("save")
        return real_save(*args, **kwargs)

    monkeypatch.setattr(jobs, "save_jobs", save)
    monkeypatch.setattr(jobs, "_write_retired_oneshot_diagnostic", lambda *args: event("retired_notice"))
    monkeypatch.setattr(jobs, "save_job_output", lambda *args: event("output"))
    monkeypatch.setattr(owner_alerts, "queue_owner_failure", lambda *args, **kwargs: event("missed_notice"))
    monkeypatch.setattr(jobs.store_health, "outage_covers", lambda *args: scenario == "outage")
    monkeypatch.setattr(jobs.store_health, "end_recovery_window", lambda *args: event("recovery_end"))

    def run(prefetch):
        real_save([job])
        events.clear()
        if prefetch:
            result = jobs.get_due_jobs()
        else:
            with jobs._jobs_lock():
                result = jobs._get_due_jobs_locked()
        return result, jobs.load_jobs(), list(events)

    assert run(True) == run(False)
    assert all(depth == 1 for _name, depth in events), events


def test_prefetch_reduces_locked_ledger_reads_to_due_jobs(store, monkeypatch, record_property):
    job, now = store
    records = [dict(job, id=f"future-{i}", next_run_at=(now + timedelta(hours=1)).isoformat())
               for i in range(200)] + [dict(job, id=f"due-{i}") for i in range(2)]
    real = occurrences.completed_occurrence
    locked = []

    def lookup(*args):
        if getattr(jobs._jobs_lock_state, "depth", 0):
            locked.append(args[0]["id"])
        return real(*args)

    monkeypatch.setattr(occurrences, "completed_occurrence", lookup)
    jobs.save_jobs(records, removed_ids={"due"})
    with jobs._jobs_lock():
        baseline = jobs._get_due_jobs_locked()
    base_count = len(locked)
    jobs.save_jobs(records, removed_ids={"due"})
    locked.clear()
    candidate = jobs.get_due_jobs()
    head_count = len(locked)
    assert {j["id"] for j in baseline} == {j["id"] for j in candidate} == {"due-0", "due-1"}
    assert (base_count, head_count) == (202, 2)
    record_property("locked_ledger_calls_base", base_count)
    record_property("locked_ledger_calls_head", head_count)
    record_property("jobs", 202)
    record_property("due", 2)
    print(f"LOCKED_LEDGER_CALLS base={base_count} head={head_count} jobs=202 due=2")


@pytest.mark.parametrize("instant", ["0001-01-01T00:00:00+01:00", "0001-01-01T00:00:00+00:00"])
def test_malformed_utc_overflow_cannot_abort_healthy_siblings(store, instant):
    job, _now = store
    malformed = dict(job, id="overflow", next_run_at=instant)
    jobs.save_jobs([malformed, job])
    assert "due" in {row["id"] for row in jobs.get_due_jobs()}
