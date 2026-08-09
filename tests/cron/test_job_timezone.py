"""Per-job timezone contracts for cron scheduling and persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from cron.jobs import (
    advance_next_run,
    claim_job_for_fire,
    compute_next_run,
    create_job,
    get_due_jobs,
    get_job,
    list_jobs,
    load_jobs,
    mark_job_run,
    pause_job,
    resume_job,
    save_jobs,
    update_job,
)


@pytest.fixture()
def cron_store(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


@pytest.fixture()
def fixed_now(monkeypatch):
    current = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: current)
    return current


def _absolute(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def test_create_rejects_invalid_timezone_before_persistence(cron_store):
    with pytest.raises(ValueError, match="Invalid IANA timezone"):
        create_job("test", "0 9 * * *", timezone="Mars/Olympus_Mons")

    assert load_jobs() == []


def test_update_rejects_invalid_timezone_without_mutating_job(cron_store):
    job = create_job("test", "0 9 * * *", timezone="America/New_York")

    with pytest.raises(ValueError, match="Invalid IANA timezone"):
        update_job(job["id"], {"timezone": "Mars/Olympus_Mons"})

    assert get_job(job["id"])["timezone"] == "America/New_York"


def test_same_wall_clock_in_distinct_zones_is_distinct_absolute_instant(fixed_now):
    schedule = {"kind": "cron", "expr": "0 9 * * *"}

    new_york = compute_next_run(schedule, timezone="America/New_York")
    los_angeles = compute_next_run(schedule, timezone="America/Los_Angeles")

    ny_dt = datetime.fromisoformat(new_york)
    la_dt = datetime.fromisoformat(los_angeles)
    assert ny_dt.hour == la_dt.hour == 9
    assert _absolute(new_york) != _absolute(los_angeles)
    assert _absolute(new_york) == datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    assert _absolute(los_angeles) == datetime(2026, 1, 1, 17, 0, tzinfo=timezone.utc)


def test_missing_pin_preserves_profile_timezone_behavior(monkeypatch):
    tokyo = ZoneInfo("Asia/Tokyo")
    current = datetime(2026, 1, 1, 12, 0, tzinfo=tokyo)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: current)

    result = compute_next_run({"kind": "cron", "expr": "0 9 * * *"})

    parsed = datetime.fromisoformat(result)
    assert parsed.utcoffset() == current.utcoffset()
    assert parsed == datetime(2026, 1, 2, 9, 0, tzinfo=tokyo)


def test_update_and_clear_timezone_recalculate_without_resuming_paused_job(
    cron_store, fixed_now
):
    job = create_job("test", "0 9 * * *", timezone="America/New_York")
    ny_next = job["next_run_at"]
    paused = pause_job(job["id"], reason="hold")

    changed = update_job(job["id"], {"timezone": "America/Los_Angeles"})
    assert changed["timezone"] == "America/Los_Angeles"
    assert changed["next_run_at"] != ny_next
    assert changed["enabled"] is False
    assert changed["state"] == "paused"

    cleared = update_job(job["id"], {"timezone": ""})
    assert cleared["timezone"] is None
    assert _absolute(cleared["next_run_at"]) == datetime(
        2026, 1, 2, 9, 0, tzinfo=timezone.utc
    )
    assert cleared["enabled"] is False
    assert cleared["state"] == "paused"

    rescheduled = update_job(job["id"], {"schedule": "0 10 * * *"})
    assert _absolute(rescheduled["next_run_at"]) == datetime(
        2026, 1, 2, 10, 0, tzinfo=timezone.utc
    )
    assert rescheduled["enabled"] is False
    assert rescheduled["state"] == "paused"

    resumed = resume_job(job["id"])
    assert resumed["timezone"] is None
    assert resumed["enabled"] is True
    assert resumed["state"] == "scheduled"
    assert resumed["next_run_at"] == rescheduled["next_run_at"]
    assert paused["state"] == "paused"


def test_timezone_does_not_change_interval_or_absolute_oneshot_semantics(
    cron_store, fixed_now
):
    ny_interval = create_job("interval", "every 2h", timezone="America/New_York")
    la_interval = create_job("interval", "every 2h", timezone="America/Los_Angeles")
    assert ny_interval["next_run_at"] == la_interval["next_run_at"]

    run_at = "2026-01-02T09:00:00+05:30"
    one_shot = create_job("once", run_at, timezone="America/Los_Angeles")
    assert one_shot["schedule"]["run_at"] == run_at
    assert one_shot["next_run_at"] == run_at


def test_spring_gap_and_fall_repeat_follow_croniter_zoneinfo_without_double_run(
    monkeypatch,
):
    spring_base = datetime(2026, 3, 7, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: spring_base)
    spring = compute_next_run(
        {"kind": "cron", "expr": "30 2 * * *"},
        timezone="America/New_York",
    )
    # croniter returns the nonexistent 02:30 with the pre-gap offset; as an
    # absolute instant ZoneInfo normalizes it to 03:30 EDT.
    assert _absolute(spring) == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)
    assert datetime.fromisoformat(spring).astimezone(
        ZoneInfo("America/New_York")
    ).hour == 3

    fall_base = datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: fall_base)
    first = compute_next_run(
        {"kind": "cron", "expr": "30 1 * * *"},
        timezone="America/New_York",
    )
    second = compute_next_run(
        {"kind": "cron", "expr": "30 1 * * *"},
        last_run_at=first,
        timezone="America/New_York",
    )
    assert _absolute(first) == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    assert _absolute(second) > _absolute(first)
    assert datetime.fromisoformat(second).date() > datetime.fromisoformat(first).date()


def test_persist_load_restart_round_trip_and_legacy_record_stays_keyless(
    cron_store, fixed_now
):
    created = create_job("pinned", "0 9 * * *", timezone="America/New_York")
    assert load_jobs()[0]["timezone"] == "America/New_York"
    assert get_job(created["id"])["timezone"] == "America/New_York"

    legacy = {
        "id": "legacy",
        "name": "legacy",
        "prompt": "legacy",
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "schedule_display": "0 9 * * *",
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2026-01-02T09:00:00+00:00",
    }
    save_jobs([legacy])
    listed = list_jobs()
    assert "timezone" not in listed[0]
    assert "timezone" not in load_jobs()[0]


def test_recovery_advance_and_completion_use_explicit_timezone(
    cron_store, monkeypatch
):
    current = {"value": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: current["value"])
    job = create_job("test", "0 9 * * *", timezone="America/New_York")
    stored = load_jobs()
    stored[0].pop("next_run_at")
    save_jobs(stored)

    assert get_due_jobs() == []
    recovered = get_job(job["id"])
    assert _absolute(recovered["next_run_at"]) == datetime(
        2026, 1, 1, 14, 0, tzinfo=timezone.utc
    )

    current["value"] = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    assert advance_next_run(job["id"])
    advanced = get_job(job["id"])
    assert _absolute(advanced["next_run_at"]) == datetime(
        2026, 1, 2, 14, 0, tzinfo=timezone.utc
    )

    mark_job_run(job["id"], True)
    completed = get_job(job["id"])
    assert _absolute(completed["next_run_at"]) == datetime(
        2026, 1, 2, 14, 0, tzinfo=timezone.utc
    )


def test_due_migration_repair_compares_against_explicit_zone(
    cron_store, monkeypatch
):
    # At 06:30 Pacific the 09:00 New York occurrence is already 30 minutes
    # past. Comparing its -05 offset/wall clock against the profile's -08
    # clock would misclassify it as a timezone migration and skip it.
    current = datetime(2026, 1, 1, 6, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: current)
    job = create_job("test", "0 9 * * *", timezone="America/New_York")
    stored = load_jobs()
    stored[0]["next_run_at"] = "2026-01-01T09:00:00-05:00"
    save_jobs(stored)

    due = get_due_jobs()

    assert [item["id"] for item in due] == [job["id"]]


def test_fast_forward_and_external_fire_claim_use_explicit_zone(
    cron_store, monkeypatch
):
    current = {"value": datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: current["value"])
    job = create_job("test", "0 9 * * *", timezone="America/New_York")
    stored = load_jobs()
    stored[0]["next_run_at"] = "2026-01-01T09:00:00-05:00"
    save_jobs(stored)

    assert [item["id"] for item in get_due_jobs()] == [job["id"]]
    fast_forwarded = get_job(job["id"])
    assert _absolute(fast_forwarded["next_run_at"]) == datetime(
        2026, 1, 3, 14, 0, tzinfo=timezone.utc
    )

    current["value"] = datetime(2026, 1, 3, 15, 0, tzinfo=timezone.utc)
    assert claim_job_for_fire(job["id"])
    claimed = get_job(job["id"])
    assert _absolute(claimed["next_run_at"]) == datetime(
        2026, 1, 4, 14, 0, tzinfo=timezone.utc
    )
