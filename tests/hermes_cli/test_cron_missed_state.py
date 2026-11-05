"""Held one-shots stay visible, require a decision, and are never completion-pruned."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from cron import jobs
from hermes_cli import cron


@pytest.mark.parametrize("enabled", [False, True])
def test_missed_is_a_hold_not_a_completed_or_hidden_job(tmp_path, monkeypatch, capsys, enabled):
    now = datetime(2026, 10, 29, 17, 21, 28, tzinfo=UTC)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    monkeypatch.setattr("hermes_time.now", lambda: now)
    monkeypatch.setattr(cron, "_warn_if_gateway_not_running", lambda: None)
    due = (now - timedelta(seconds=600)).isoformat()
    job = {"id": "held", "name": "held", "prompt": "payload", "enabled": enabled, "state": "missed",
           "schedule": {"kind": "once", "run_at": due}, "next_run_at": due,
           "repeat": {"times": 1, "completed": 0}, "deliver": "local",
           "last_run_at": (now - timedelta(days=365)).isoformat()}
    jobs.save_jobs([job])
    assert jobs.effective_job_state(job) == "missed"
    assert jobs.is_terminal_job(job), "held is terminal for automatic scheduling, not completed"
    assert not jobs.is_job_runnable(job)
    assert jobs.get_due_jobs() == []
    assert jobs.get_job("held")["state"] == "missed", "completion retention must never discard a held payload"
    assert jobs.claim_job_for_fire("held", force=True) is False
    jobs.update_job("held", {"enabled": False, "prompt": "edited payload"})
    for updates in ({"enabled": True}, {"state": "scheduled"}, {"next_run_at": now.isoformat()}):
        with pytest.raises(ValueError, match="resume"):
            jobs.update_job("held", updates)
    with pytest.raises(ValueError, match="missed.*resume"):
        jobs.trigger_job("held")
    cron.cron_list()
    out = capsys.readouterr().out
    assert "held" in out and "[missed]" in out and "owner decision" in out
    assert cron._next_run_row(job)[0] == "Missed"
    assert cron.cron_doctor() == 1
    out = capsys.readouterr().out
    assert "held" in out and "owner decision" in out and "scheduler running" not in out
    assert cron.cron_resume(SimpleNamespace(job_id="held", run_at=None, run_now=True)) == 0
    resumed = jobs.get_job("held")
    assert resumed["state"] == "scheduled" and resumed["enabled"] is True
    assert resumed["repeat"]["completed"] == 0 and resumed["prompt"] == "edited payload"
