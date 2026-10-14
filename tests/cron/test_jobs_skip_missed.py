"""Explicit skip of cron occurrences: resume --skip-missed, skip_job(count), paused edit re-anchor.

Default resume keeps a paused-over slot due (#113603); that pin stays in tests/cron/test_jobs.py
(`test_resume_keeps_slot_that_elapsed_while_paused_due`). These cover the opt-outs.
"""

import pytest
from datetime import datetime, timedelta, timezone

from cron.jobs import create_job, get_due_jobs, get_job, pause_job, resume_job, skip_job, update_job


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class TestSkipMissedOccurrences:
    """Explicit skip paths."""

    # Tuesday 2026-10-13 21:00 UTC: the 20:00 slot of ``0 20 * * 2`` has just elapsed.
    NOW = datetime(2026, 10, 13, 21, 0, 0, tzinfo=timezone.utc)
    MISSED = datetime(2026, 10, 13, 20, 0, 0, tzinfo=timezone.utc)
    NEXT = datetime(2026, 10, 20, 20, 0, 0, tzinfo=timezone.utc)

    def _paused_over_slot(self, monkeypatch):
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: self.MISSED - timedelta(days=1))
        job = create_job(prompt="weekly review", schedule="0 20 * * 2", deliver="local")
        assert datetime.fromisoformat(job["next_run_at"]) == self.MISSED
        pause_job(job["id"], reason="skip this week")
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: self.NOW)
        return job["id"]

    def test_resume_skip_missed_lands_on_next_future_slot_and_does_not_fire(
            self, tmp_cron_dir, monkeypatch, caplog):
        jid = self._paused_over_slot(monkeypatch)
        with caplog.at_level("INFO", logger="cron.jobs"):
            resumed = resume_job(jid, skip_missed=True)
        assert datetime.fromisoformat(resumed["next_run_at"]) == self.NEXT
        assert resumed["state"] == "scheduled" and resumed["enabled"] is True
        assert jid not in {j["id"] for j in get_due_jobs()}
        assert any("skipped occurrence" in r.getMessage() and self.MISSED.isoformat()[:16]
                   in r.getMessage() for r in caplog.records)

    def test_default_resume_still_keeps_the_paused_over_slot_due(self, tmp_cron_dir, monkeypatch):
        jid = self._paused_over_slot(monkeypatch)
        assert datetime.fromisoformat(resume_job(jid)["next_run_at"]) == self.MISSED
        assert jid in {j["id"] for j in get_due_jobs()}

    def test_skip_count_two_lands_on_second_future_slot(self, tmp_cron_dir, monkeypatch):
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: self.NOW)
        job = create_job(prompt="weekly review", schedule="0 20 * * 2", deliver="local")
        assert datetime.fromisoformat(job["next_run_at"]) == self.NEXT
        skipped = skip_job(job["id"], count=2)
        assert datetime.fromisoformat(skipped["next_run_at"]) == self.NEXT + timedelta(days=14)
        assert datetime.fromisoformat(skip_job(job["id"])["next_run_at"]) == self.NEXT + timedelta(days=21)

    def test_skip_refuses_paused_and_bad_count(self, tmp_cron_dir, monkeypatch):
        jid = self._paused_over_slot(monkeypatch)
        with pytest.raises(ValueError, match="not scheduled"):
            skip_job(jid)
        with pytest.raises(ValueError, match="positive integer"):
            skip_job(jid, count=0)

    def test_paused_edit_same_schedule_recomputes_to_next_future_slot(self, tmp_cron_dir, monkeypatch):
        jid = self._paused_over_slot(monkeypatch)
        assert datetime.fromisoformat(get_job(jid)["next_run_at"]) == self.MISSED
        edited = update_job(jid, {"schedule": "0 20 * * 2"})
        assert edited["state"] == "paused"
        assert datetime.fromisoformat(edited["next_run_at"]) == self.NEXT
        assert jid not in {j["id"] for j in get_due_jobs()}
        assert datetime.fromisoformat(resume_job(jid)["next_run_at"]) == self.NEXT
        assert jid not in {j["id"] for j in get_due_jobs()}
