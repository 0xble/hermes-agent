"""Per-job one-shot catch-up allowance contracts; no real dispatch or live store."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cron import jobs, jobs_oneshot, missed_windows
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
    return {
        "id": "once", "name": "once", "prompt": "do not send stale payload",
        "schedule": {"kind": "once", "run_at": due.isoformat()},
        "next_run_at": due.isoformat(), "last_run_at": None,
        "enabled": True, "state": "scheduled",
        "repeat": {"times": 1, "completed": 0}, "deliver": "local", **extra,
    }


@pytest.mark.parametrize("value, expected", [
    (None, 120), ("", 120), (0, 120), (119, 120), (300, 300), (300.5, 300.5),
    (-1, 120), (True, 120), ("hello", 120), (float("inf"), 120), (float("nan"), 120),
])
def test_oneshot_grace_defaults_to_120_and_only_widens(value, expected):
    assert jobs.oneshot_grace_seconds({FIELD: value}) == expected
    assert jobs.oneshot_grace_seconds({}) == 120


@pytest.mark.parametrize("age, allowance", [(300, 600), (899, 900)])
def test_per_job_allowance_keeps_one_shot_due(store, monkeypatch, age, allowance):
    job = oneshot(age, **{FIELD: allowance})
    jobs_oneshot.record_due_scan(NOW - timedelta(seconds=1))
    retirement = Mock(wraps=jobs_oneshot.retire_expired_oneshot)
    monkeypatch.setattr(jobs_oneshot, "retire_expired_oneshot", retirement)
    monkeypatch.setattr(missed_windows, "claim_slot", Mock(side_effect=AssertionError("once slot")))
    jobs.save_jobs([job])
    assert [item["id"] for item in jobs.get_due_jobs()] == ["once"]
    assert retirement.call_count == 1
    saved = jobs.load_jobs()[0]
    assert saved["state"] == "scheduled" and saved["enabled"] is True


def test_retired_oneshot_diagnostic_reports_actual_allowance(store):
    job = oneshot(601, **{FIELD: 600})
    from cron.jobs_oneshot_diagnostics import write_retired

    write_retired(job, job["next_run_at"])  # the writer both retirement gates call
    diagnostic = next((store / "cron/output/once").glob("*.md"))
    assert "grace window: 600s" in diagnostic.read_text()
    assert "grace window: 120s" not in diagnostic.read_text()


@pytest.mark.parametrize("age, allowance", [(601, 600), (901, 900), (6 * 60 * 60, 900)])
def test_per_job_allowance_holds_after_deadline_with_actual_message(store, age, allowance):
    job = oneshot(age, **{FIELD: allowance})
    jobs.save_jobs([job])
    assert jobs.get_due_jobs() == []
    saved = jobs.load_jobs()[0]
    assert saved["state"] == "missed" and saved["enabled"] is False
    assert saved["prompt"] == job["prompt"]
    diagnostic = next((store / "cron/output/once").glob("*.md"))
    assert f"{allowance}-second grace window" in diagnostic.read_text()


@pytest.mark.parametrize("field, age, due", [(None, 119, True), (0, 119, True), (0, 121, False)])
def test_unset_and_zero_allowance_keep_historical_120_second_boundary(store, field, age, due):
    extra = {} if field is None else {FIELD: field}
    jobs.save_jobs([oneshot(age, **extra)])
    assert bool(jobs.get_due_jobs()) is due
    if not due:
        diagnostic = next((store / "cron/output/once").glob("*.md"))
        assert "120-second grace window" in diagnostic.read_text()


def test_stall_continuity_uses_the_same_widened_allowance(store):
    # Prior scan is 150s after the slot: inside 300s but outside the old 120s.
    jobs_oneshot.record_due_scan(NOW - timedelta(seconds=50))
    descriptor = SimpleNamespace(scan=SimpleNamespace(now=NOW),
                                 next_run_dt=NOW - timedelta(seconds=200), job={FIELD: 300})
    assert jobs_oneshot._stall_covers(descriptor) is True
    descriptor.job = {}
    assert jobs_oneshot._stall_covers(descriptor) is False


def test_outage_provenance_uses_the_same_widened_allowance(store, monkeypatch):
    covers = Mock(return_value=False)
    monkeypatch.setattr(jobs.store_health, "outage_covers", covers)
    jobs.save_jobs([oneshot(301, **{FIELD: 300})])
    assert jobs.get_due_jobs() == []
    assert covers.call_args.args[2] == 300


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("age, due", [(200, True), (301, False)])
def test_missing_slot_recovery_applies_allowance_without_losing_misses(store, missing, age, due):
    job = oneshot(age, **{FIELD: 300})
    if missing:
        job.pop("next_run_at")
    jobs.save_jobs([job])
    assert bool(jobs.get_due_jobs()) is due
    saved = jobs.load_jobs()[0]
    assert saved["state"] == ("scheduled" if due else "missed")
    if due and missing:
        assert saved["next_run_at"] == job["schedule"]["run_at"]


def test_recovery_keeps_fixed_create_update_resume_allowance():
    job = oneshot(200, **{FIELD: 900})
    assert jobs._recoverable_oneshot_run_at(
        job["schedule"], NOW, last_run_at=None,
    ) is None


def test_recurring_expiration_remains_separate():
    job = oneshot(301, **{FIELD: 300})
    assert missed_windows.expired_occurrence(job, job["next_run_at"], NOW) is None
    job["schedule"] = {"kind": "cron", "expr": "* * * * *"}
    result = missed_windows.expired_occurrence(job, job["next_run_at"], NOW)
    assert result["lateness_seconds"] == 301 and result[FIELD] == 300
    job[FIELD] = 0  # Unlike once, zero remains a strict deadline for recurring jobs.
    assert missed_windows.expired_occurrence(job, job["next_run_at"], NOW)[FIELD] == 0


@pytest.mark.parametrize("action", ["create", "update", "resume"])
def test_acceptance_of_already_past_jobs_stays_at_120_seconds(store, action):
    late = oneshot(200, **{FIELD: 300})
    with pytest.raises(ValueError, match="120"):
        if action == "create":
            jobs.create_job("payload", late["next_run_at"], max_catch_up_lateness_seconds=300)
        elif action == "update":
            jobs.save_jobs([oneshot(-60, **{FIELD: 300})])
            jobs.update_job("once", {"schedule": late["schedule"]})
        else:
            late.update(state="paused", enabled=False, paused_at=NOW.isoformat())
            jobs.save_jobs([late])
            jobs.resume_job("once")


@pytest.mark.parametrize("age, claimed", [(200, True), (300, True), (400, False)])
def test_misfire_backstop_honors_per_job_allowance(store, monkeypatch, age, claimed):
    monkeypatch.setattr("cron.scheduler_provider._misfire_grace_minutes", lambda: 0.5)
    thread = Mock()
    monkeypatch.setattr("cron.scheduler_provider.threading.Thread", thread)
    monkeypatch.setattr(jobs, "load_jobs", lambda: [oneshot(age, **{FIELD: 300})])
    provider = Mock()
    provider.claim_fire.return_value = {"id": "once"}
    assert fire_overdue_jobs(provider, now=NOW) == int(claimed)
    assert provider.claim_fire.call_count == int(claimed)
    assert thread.call_count == int(claimed)
