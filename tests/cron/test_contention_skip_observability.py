"""Durable observability for cron contention skips."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


def test_already_running_skip_persists_reason(tmp_path, monkeypatch):
    import cron.jobs as jobs
    import cron.scheduler as scheduler

    home = tmp_path / ".hermes"
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(jobs, "HERMES_DIR", home)
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    monkeypatch.setattr(scheduler, "_hermes_home", home)
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda: [])

    script = home / "scripts" / "probe.py"
    script.parent.mkdir()
    script.write_text("print('ok')\n")
    job = jobs.create_job(
        prompt="contention", schedule="every 5m", no_agent=True, script=str(script))
    jobs.update_job(job["id"], {
        "next_run_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    })

    # Exercise the same guard branch used by the ticker without starting a worker.
    scheduler._running_job_ids.clear()
    scheduler._running_since.clear()
    scheduler._running_futures.clear()
    assert scheduler.try_register_running_job(job["id"])
    try:
        assert scheduler._submit_with_guard(
            jobs.get_job(job["id"]),
            scheduler._get_parallel_pool(1),
            lambda _job: None,
        ) is None
    finally:
        scheduler.release_running_job(job["id"])
        scheduler._shutdown_parallel_pool()

    stored = json.loads((cron_dir / "jobs.json").read_text())
    row = next(item for item in stored["jobs"] if item["id"] == job["id"])
    assert row["last_skip_reason"] == "already_running"
    assert row["last_skipped_at"]


def test_completed_run_clears_the_persisted_skip():
    """The second I6 review: a single contention skip showed the callout forever."""
    import cron.jobs as jobs
    job = {"id": "j1", "last_skipped_at": "2026-09-01T09:00:00+00:00", "last_skip_reason": "already_running"}
    jobs._record_run_outcome(job, True, None, None, None, "2026-09-02T09:00:00+00:00")
    assert "last_skipped_at" not in job and "last_skip_reason" not in job
