"""Manual admission must not consume a schedule before its executor starts."""

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture
def job_store(tmp_path, monkeypatch):
    from cron import jobs

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    clock = [datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock[0])
    return jobs, clock


@pytest.mark.parametrize("schedule", ["every 5m", "*/5 * * * *", "in 5m"])
@pytest.mark.parametrize("paused", [False, True])
def test_manual_setup_failure_keeps_pending_occurrence(job_store, monkeypatch, schedule, paused):
    from cron import scheduler
    from tools.cronjob_tools import _execute_job_now

    jobs, clock = job_store
    job = jobs.create_job(prompt="pending", schedule=schedule, timezone="UTC")
    if paused:
        jobs.pause_job(job["id"], reason="operator hold")
    before = jobs.get_job(job["id"])
    clock[0] += timedelta(minutes=6)

    def setup_failure():
        raise RuntimeError("gateway context unavailable before executor entry")

    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_gateway_runner_ref=setup_failure))
    result = _execute_job_now(before)
    assert result["claimed"] is True
    assert result["executed"] is False
    assert result["success"] is False
    assert "before executor entry" in result["error"]
    after = jobs.get_job(job["id"])
    assert after["next_run_at"] == before["next_run_at"]
    assert after["repeat"] == before["repeat"]
    assert after["last_run_at"] == before["last_run_at"]
    assert after["fire_claim"] is None
    assert job["id"] not in scheduler.get_running_job_ids()
    due_ids = {due["id"] for due in jobs.get_due_jobs()}
    if paused:
        assert after["state"] == "paused"
        assert after["paused_reason"] == "operator hold"
        assert job["id"] not in due_ids
    else:
        assert job["id"] in due_ids


@pytest.mark.parametrize("claim_kwargs", [{"manual": True}, {"force": True}])
def test_explicit_manual_claim_preserves_until_normal_completion(job_store, claim_kwargs):
    jobs, clock = job_store
    job = jobs.create_job(prompt="manual", schedule="every 5m")
    before = jobs.get_job(job["id"])
    clock[0] += timedelta(minutes=3)
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True, **claim_kwargs)
    assert isinstance(claimed, dict)
    assert claimed["next_run_at"] == before["next_run_at"]
    assert claimed["_scheduled_instant"] is None
    owner = claimed["fire_claim"]["by"]
    assert jobs.claim_job_for_fire(job["id"], manual=True) is False
    assert jobs.release_fire_claim(job["id"], expected_owner="wrong-owner") is False
    clock[0] += timedelta(minutes=1)
    assert jobs.mark_job_run(job["id"], True, expected_fire_owner=owner) is True
    after = jobs.get_job(job["id"])
    assert after["next_run_at"] == (clock[0] + timedelta(minutes=5)).isoformat()
    assert after["repeat"]["completed"] == before["repeat"]["completed"] + 1
    assert after["last_status"] == "ok"
    assert after["fire_claim"] is None


def test_scheduled_and_tick_trigger_claims_still_advance(job_store):
    jobs, clock = job_store
    scheduled = jobs.create_job(prompt="scheduled", schedule="every 5m")
    triggered = jobs.create_job(prompt="triggered", schedule="every 5m")
    clock[0] += timedelta(minutes=6)
    jobs.trigger_job(triggered["id"])
    for job in (scheduled, triggered):
        before = jobs.get_job(job["id"])
        claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
        assert isinstance(claimed, dict)
        assert claimed["next_run_at"] != before["next_run_at"]
        assert claimed["next_run_at"] == (clock[0] + timedelta(minutes=5)).isoformat()


def test_manual_abort_preserves_concurrent_schedule_edit(job_store, monkeypatch):
    from tools.cronjob_tools import _execute_job_now

    jobs, clock = job_store
    job = jobs.create_job(prompt="manual", schedule="every 5m")
    clock[0] += timedelta(minutes=3)
    edited = []

    def setup_failure():
        edited.append(jobs.update_job(job["id"], {"schedule": "every 20m"}))
        raise RuntimeError("setup failed after schedule edit")

    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_gateway_runner_ref=setup_failure))
    result = _execute_job_now(job)
    assert result["executed"] is False
    after = jobs.get_job(job["id"])
    assert after["next_run_at"] == edited[0]["next_run_at"]
    assert after["schedule"] == edited[0]["schedule"]
    assert after["fire_claim"] is None
