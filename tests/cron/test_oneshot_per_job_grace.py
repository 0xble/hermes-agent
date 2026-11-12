"""Per-job one-shot catch-up allowance contracts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from cron import jobs, jobs_oneshot
from cron.scheduler_provider import fire_overdue_jobs


NOW = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
FIELD = "max_catch_up_lateness_seconds"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron/jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron/output")
    monkeypatch.setattr(jobs, "_hermes_now", lambda: NOW)
    return tmp_path


def oneshot(age, **extra):
    due = NOW - timedelta(seconds=age)
    job = {
        "id": "once",
        "name": "once",
        "prompt": "x",
        "schedule": {"kind": "once", "run_at": due.isoformat()},
        "next_run_at": due.isoformat(),
        "last_run_at": None,
        "enabled": True,
        "state": "scheduled",
        "repeat": {"times": 1, "completed": 0},
        "deliver": "local",
    }
    job.update(extra)
    return job


def test_oneshot_grace_defaults_to_120_and_only_widens():
    assert jobs.oneshot_grace_seconds({}) == 120
    assert jobs.oneshot_grace_seconds({FIELD: 0}) == 120
    assert jobs.oneshot_grace_seconds({FIELD: 300}) == 300


@pytest.mark.parametrize("age", [150, 200])
def test_per_job_allowance_keeps_one_shot_due(store, age):
    job = oneshot(age, **{FIELD: 300})
    jobs.save_jobs([job])
    assert [item["id"] for item in jobs.get_due_jobs()] == ["once"]


def test_per_job_allowance_holds_after_deadline_with_actual_message(store):
    job = oneshot(301, **{FIELD: 300})
    jobs.save_jobs([job])
    assert jobs.get_due_jobs() == []
    saved = jobs.load_jobs()[0]
    assert saved["state"] == "missed"
    assert "300-second grace window" in saved["missed_reason"]


def test_unset_allowance_keeps_historical_120_second_boundary(store):
    jobs.save_jobs([oneshot(119)])
    assert [item["id"] for item in jobs.get_due_jobs()] == ["once"]

    jobs.save_jobs([oneshot(121)])
    assert jobs.get_due_jobs() == []
    assert "120-second grace window" in jobs.load_jobs()[0]["missed_reason"]


def test_widened_allowance_still_holds_hours_late(store):
    jobs.save_jobs([oneshot(3 * 60 * 60, **{FIELD: 300})])
    assert jobs.get_due_jobs() == []
    assert jobs.load_jobs()[0]["state"] == "missed"


def test_stall_continuity_uses_the_same_widened_allowance(monkeypatch):
    run_at = NOW - timedelta(seconds=200)
    marker = NOW - timedelta(seconds=100)
    monkeypatch.setattr(jobs, "_read_marker_fields", lambda _name: [
        str(marker.timestamp()), "0", jobs_oneshot._SCAN_PROCESS_ID,
    ])
    monkeypatch.setattr(jobs_oneshot.os, "getpid", lambda: 0)
    descriptor = SimpleNamespace(
        scan=SimpleNamespace(now=NOW),
        next_run_dt=run_at,
        job={FIELD: 300},
    )
    assert jobs_oneshot._stall_covers(descriptor) is True


class RecordingProvider:
    def __init__(self):
        self.claimed = []

    def claim_fire(self, job_id):
        self.claimed.append(job_id)
        return {"id": job_id}

    def fire_claimed(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("should not run in this contract test")


def test_misfire_backstop_honors_per_job_allowance(monkeypatch):
    monkeypatch.setattr("cron.scheduler_provider._misfire_grace_minutes", lambda: 5.0)
    monkeypatch.setattr(
        "cron.scheduler_provider.threading.Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: None),
    )
    monkeypatch.setattr("cron.jobs.is_job_runnable", lambda _job: True)
    now = jobs._hermes_now()
    provider = RecordingProvider()
    eligible = oneshot(200, **{FIELD: 300})
    monkeypatch.setattr("cron.jobs.load_jobs", lambda: [eligible])
    assert fire_overdue_jobs(provider) == 1
    assert provider.claimed == ["once"]

    provider = RecordingProvider()
    expired = oneshot(400, **{FIELD: 300})
    monkeypatch.setattr("cron.jobs.load_jobs", lambda: [expired])
    assert fire_overdue_jobs(provider) == 0
    assert provider.claimed == []
    assert (now - datetime.fromisoformat(expired["next_run_at"])).total_seconds() > 300
