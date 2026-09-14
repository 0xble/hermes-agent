"""Contention cooldowns use elapsed seconds across profile-local DST changes."""
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.parametrize("instant,delay", [
    ("2026-11-01T05:30:00+00:00", 3600),  # first 01:30 through the repeated hour
    ("2026-11-01T06:15:00+00:00", 30),    # second 01:15 must not lose fold=1
    ("2026-03-08T06:45:00+00:00", 1800),  # spring gap must serialize a real local time
])
def test_persisted_deferral_is_due_only_after_elapsed_cooldown(tmp_path, monkeypatch, instant, delay):
    from cron import executions, jobs, scheduler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    payload = {"defer": {"reason": "writer busy", "retry_after_seconds": delay}}
    (scripts / "gate.py").write_text("print(" + repr(json.dumps(payload)) + ")\n")
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(scheduler, "_open_cron_session_db", lambda *a: pytest.fail("agent reached"))
    zone = ZoneInfo("America/New_York")
    started = datetime.fromisoformat(instant).astimezone(zone)
    clock = [started]
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock[0])
    job = jobs.create_job(prompt="Pending maintenance", schedule="every 1h", script="gate.py", deliver="local")
    jobs.update_job(job["id"], {"next_run_at": started.isoformat()})
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    before = jobs.get_job(job["id"])
    assert scheduler.run_one_job(claimed)

    # Reload the real jobs.json and execution database, not the in-memory claim.
    saved = jobs.get_job(job["id"])
    retry_at = datetime.fromisoformat(saved["deferred_run"]["retry_at"])
    assert retry_at.timestamp() - started.timestamp() == delay
    assert retry_at.astimezone(zone).isoformat() == retry_at.isoformat()
    assert saved["next_run_at"] == saved["deferred_run"]["retry_at"]
    assert saved["repeat"] == before["repeat"]
    assert executions.get_execution(claimed["execution_id"])["status"] == "deferred"
    assert jobs.get_due_jobs() == []
    assert jobs.claim_job_for_fire(job["id"], return_job=True) is False

    clock[0] = datetime.fromtimestamp(started.timestamp() + delay - 1, zone)
    assert jobs.get_due_jobs() == []
    assert jobs.claim_job_for_fire(job["id"], return_job=True) is False
    clock[0] = datetime.fromtimestamp(started.timestamp() + delay, zone)
    assert [due["id"] for due in jobs.get_due_jobs()] == [job["id"]]
    retry = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(retry, dict)
    assert retry["fire_claim"]["run_id"] != claimed["fire_claim"]["run_id"]
    assert retry.get("_scheduled_instant") == claimed.get("_scheduled_instant")
