"""A completion published after preflight must retain the exact locked dedup gate."""
from datetime import UTC, datetime

from cron import executions, jobs, occurrences


def test_stale_negative_prefetch_rechecks_completion_before_claim(tmp_path, monkeypatch):
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    job = {"id": "restored", "enabled": True, "state": "scheduled", "prompt": "test",
           "schedule": {"kind": "interval", "minutes": 60}, "next_run_at": now.isoformat(),
           "repeat": {"times": None, "completed": 0}}
    jobs.save_jobs([job])  # independently restored pre-fire record
    execution = executions.create_execution("restored", source="test", scheduled_instant=now.isoformat())
    real = occurrences.completed_occurrence
    calls = []

    def lookup(*args):
        depth = getattr(jobs._jobs_lock_state, "depth", 0)
        result = real(*args)
        calls.append((depth, result))
        if len(calls) == 1:
            # Real runner publication is outside mark_job_run's per-job fire fence.
            executions.finish_execution(execution["id"], success=True)
        return result

    monkeypatch.setattr(occurrences, "completed_occurrence", lookup)
    assert jobs.claim_job_for_fire("restored") is False
    assert calls == [(0, False), (1, True)]
    # Base's exact lookup now makes the same refusal; the rollback cannot undo completion.
    jobs.save_jobs([job])
    assert real(job, now.isoformat()) is True
    assert jobs.load_jobs()[0].get("fire_claim") is None
