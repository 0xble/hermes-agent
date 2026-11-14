"""Due-scan per-job containment is by job, not by exception type.

A fixed exception tuple once replaced the catch-all and let errors outside it (for example a
ledger sqlite failure during one-shot retirement) abort the whole scan, starving every healthy
sibling for that tick. These cases pin the contract for exception types beyond the usual set.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

import cron.jobs as jobs_mod
from cron.jobs import get_due_jobs, save_jobs


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


@pytest.mark.parametrize("exc", [
    sqlite3.OperationalError("database is locked"),
    ZeroDivisionError("malformed schedule"),
    LookupError("unexpected"),
], ids=["sqlite-operational", "zero-division", "lookup"])
def test_any_exception_type_skips_only_the_failing_job(tmp_cron_dir, exc):
    past = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    poison = {"id": "poison", "schedule": {"kind": "interval", "minutes": 7},
              "next_run_at": past, "enabled": True, "created_at": past}
    good = {"id": "good-sibling", "schedule": {"kind": "interval", "minutes": 5},
            "next_run_at": past, "enabled": True, "created_at": past}
    save_jobs([poison, good])
    real_grace = jobs_mod._compute_grace_seconds

    def selective_grace(schedule):
        if schedule.get("minutes") == 7:
            raise exc
        return real_grace(schedule)

    with patch.object(jobs_mod, "_compute_grace_seconds", selective_grace):
        ids = [job["id"] for job in get_due_jobs()]
    assert "good-sibling" in ids, f"healthy sibling starved: {ids}"
    assert "poison" not in ids
