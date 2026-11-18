"""Prefetch must preserve legacy-record containment and same-tick dead-owner recovery."""
import copy
import json
import socket
from datetime import UTC, datetime

import pytest

from cron import executions, jobs


@pytest.fixture
def due_store(tmp_path, monkeypatch):
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    job = {"id": "healthy", "enabled": True, "state": "scheduled", "prompt": "test",
           "schedule": {"kind": "interval", "minutes": 60}, "next_run_at": now.isoformat(),
           "repeat": {"times": None, "completed": 0}}
    return now, job


@pytest.mark.parametrize("manual", [False, True])
def test_claim_ignores_idless_legacy_sibling(due_store, manual):
    _, job = due_store
    jobs.JOBS_FILE.write_text(json.dumps({"jobs": [{"name": "legacy"}, job]}))

    assert jobs.claim_job_for_fire("healthy", manual=manual) is True
    stored = next(record for record in jobs.load_jobs() if record.get("id") == "healthy")
    assert stored["fire_claim"]["by"]


def test_new_dead_owner_after_preflight_is_released_same_tick(due_store, monkeypatch):
    from gateway import status

    now, job = due_store
    jobs.save_jobs([job])
    real_load = jobs.load_jobs
    calls = 0

    def load_then_publish_claim():
        nonlocal calls
        records = real_load()
        calls += 1
        if calls == 1:
            # Another writer publishes a fresh lease after our preflight snapshot.
            published = copy.deepcopy(job)
            published["fire_claim"] = {"at": now.isoformat(),
                                       "by": f"{socket.gethostname()}:12345:new-owner"}
            jobs.JOBS_FILE.write_text(json.dumps({"jobs": [published]}))
        return records

    monkeypatch.setattr(jobs, "load_jobs", load_then_publish_claim)
    monkeypatch.setattr(status, "_pid_exists", lambda pid: False)

    assert jobs.claim_job_for_fire("healthy") is True
    assert real_load()[0]["fire_claim"]["by"] != f"{socket.gethostname()}:12345:new-owner"
