"""An update fence that outlives the gateway restart holds agent jobs instead of storming.

On 2026-11-14 the update lock stayed held for about 2 minutes after the gateway restarted. The
ready gateway's ticker kept dispatching agent jobs whose handshake answered ``update_paused``:
each tick booked a failed "Not started; occurrence deferred" run, restored the slot, and re-fired
it on the next tick (141 failed and 50 missed rows, one occurrence restored 49 times). While the
fence is held the due scan now leaves agent jobs untouched; script jobs keep running.
"""

from datetime import UTC, datetime, timedelta

import pytest

from cron import dispatch_hold
from cron.jobs import get_due_jobs, load_jobs, save_jobs

NOW = datetime(2026, 11, 14, 16, 57, 30, tzinfo=UTC)


@pytest.fixture()
def cron_store(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: NOW)
    monkeypatch.setattr(dispatch_hold, "_hold_since", {})
    (tmp_path / "cron").mkdir()
    return tmp_path / "cron"


def _job(jid, **extra):
    job = {
        "id": jid, "name": jid, "prompt": "x",
        "schedule": {"kind": "interval", "minutes": 5},
        "next_run_at": (NOW - timedelta(seconds=30)).isoformat(),
        "last_run_at": None, "enabled": True, "state": "scheduled",
        "repeat": {"times": None, "completed": 0}, "deliver": "local",
    }
    job.update(extra)
    return job


def _fence(monkeypatch, held):
    monkeypatch.setattr("hermes_cli.gateway_runtime.update_fence_held", lambda _home: held)


def test_fenced_scan_holds_agent_jobs_untouched_and_runs_scripts(cron_store, monkeypatch):
    pending = {"scheduled_at": (NOW - timedelta(minutes=3)).isoformat(), "by": "gone", "at": "x"}
    save_jobs([_job("agent", pending_slot=pending), _job("script", no_agent=True, script="s.py")])
    before = {j["id"]: j for j in load_jobs()}["agent"]

    _fence(monkeypatch, True)
    for _tick in range(3):
        assert [j["id"] for j in get_due_jobs()] == ["script"]
        after = {j["id"]: j for j in load_jobs()}["agent"]
        # No restore, no dispatch stamp, no advance: the occurrence waits for the update to end.
        assert after["next_run_at"] == before["next_run_at"]
        assert after.get("pending_slot") == pending
        assert "last_dispatch" not in after
    assert dispatch_hold.read(cron_store) is not None  # held ticks are stamped like a drain

    _fence(monkeypatch, False)
    assert "agent" in [j["id"] for j in get_due_jobs()]
