"""A recurring occurrence that a tick took off the schedule but never dispatched must fire once
after the scheduler restarts — never be silently lost, never fire twice (#107485).

The tick advances ``next_run_at`` BEFORE dispatch (at-most-once across a crash mid-run). When the
process dies in the window between that advance and the fire claim — the interpreter was already
finalizing, the executor refused work, SIGKILL — the restarted scan used to see only the future
``next_run_at`` and the occurrence vanished: no execution row, no log line. The store now carries a
``pending_slot`` stamp across that window and a later scan restores it as the due instant.

Drives the REAL ``tick()`` against a throwaway HERMES_HOME with a ``no_agent`` script job that
appends one line per fire. Process 1 is a real subprocess that dies inside the window, so the
restarted scan sees a provably dead owner exactly as a gateway restart does.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_CRASH_BEFORE_DISPATCH = """
import os, sys
sys.path.insert(0, sys.argv[1])
import cron.scheduler as S
S.create_execution = lambda *a, **k: os._exit(137)  # SIGKILL in the advance→claim window
S.tick(verbose=False, sync=True)
"""

_REFUSE_AND_EXIT = """
import os, sys
from datetime import datetime
sys.path.insert(0, sys.argv[1])
from cron import jobs, scheduler, scheduler_admission
jobs._hermes_now = lambda: datetime.fromisoformat(sys.argv[2])

def refuse(*args):
    raise RuntimeError('cannot schedule new futures after interpreter shutdown')

scheduler_admission.submit_ranked = refuse
assert scheduler.tick(verbose=False, sync=True) == 0
os._exit(137)
"""


@pytest.fixture
def slot_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "cron" / "output").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MACHINE_ID", raising=False)

    import cron.executions as E
    import cron.jobs as J
    import cron.scheduler as S

    monkeypatch.setattr(J, "HERMES_DIR", home)
    monkeypatch.setattr(J, "CRON_DIR", home / "cron")
    monkeypatch.setattr(J, "JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr(J, "OUTPUT_DIR", home / "cron" / "output")
    monkeypatch.setattr(E, "EXECUTIONS_FILE", home / "cron" / "executions.db")
    monkeypatch.setattr(S, "_hermes_home", home)
    S._running_job_ids.clear()
    S._running_since.clear()
    S._running_futures.clear()

    counter = home / "fires.txt"
    (home / "scripts" / "fire.sh").write_text(
        f"#!/bin/sh\necho fired >> {counter}\necho fired\n", encoding="utf-8")
    (home / "scripts" / "fire.sh").chmod(0o755)
    job = J.create_job(prompt=None, schedule="every 1h", name="slot", script="fire.sh",
                       no_agent=True, deliver="local")
    slot = (J._hermes_now() - timedelta(minutes=1)).replace(microsecond=0).isoformat()
    stored = J.load_jobs()
    next(r for r in stored if r["id"] == job["id"])["next_run_at"] = slot
    J.save_jobs(stored)

    def fires() -> int:
        return counter.read_text(encoding="utf-8").count("\n") if counter.exists() else 0

    def crash_before_dispatch() -> None:
        """Process 1: its tick advances the schedule, then it dies before any fire claim."""
        env = dict(os.environ, HERMES_HOME=str(home))
        proc = subprocess.run([sys.executable, "-c", _CRASH_BEFORE_DISPATCH, str(REPO)],
                              env=env, cwd=str(REPO), capture_output=True, text=True, timeout=120, check=False)
        assert proc.returncode == 137, proc.stderr[-2000:]

    yield {"job_id": job["id"], "slot": slot, "fires": fires, "crash": crash_before_dispatch,
           "S": S, "J": J, "E": E}
    S._shutdown_parallel_pool()


class TestMissedWindowCatchUp:
    def test_slot_lost_before_dispatch_fires_once_after_restart(self, slot_env):
        S, J, E = slot_env["S"], slot_env["J"], slot_env["E"]
        job_id, slot = slot_env["job_id"], slot_env["slot"]

        slot_env["crash"]()
        after_crash = J.get_job(job_id)
        assert J._ensure_aware(J.datetime.fromisoformat(after_crash["next_run_at"])) > J._hermes_now()
        assert slot_env["fires"]() == 0

        # Restarted scheduler: the occurrence must come back and run exactly once.
        S.tick(verbose=False, sync=True)
        assert slot_env["fires"]() == 1, "occurrence lost in the restart gap must fire once"
        row = E.latest_execution(job_id)
        assert row["status"] == "completed"
        from cron.occurrences import scheduled_instant
        assert row["scheduled_instant"] == scheduled_instant(slot), "restored slot keeps its identity"

        S.tick(verbose=False, sync=True)
        assert slot_env["fires"]() == 1
        rec = J.get_job(job_id)
        assert "pending_slot" not in rec
        assert J._ensure_aware(J.datetime.fromisoformat(rec["next_run_at"])) > J._hermes_now()

    def test_restored_utc_slot_survives_another_shutdown(self, slot_env, monkeypatch):
        from datetime import UTC, datetime
        from zoneinfo import ZoneInfo

        S, J, E = slot_env["S"], slot_env["J"], slot_env["E"]
        job_id = slot_env["job_id"]
        pacific = ZoneInfo("America/Los_Angeles")
        due = datetime(2026, 11, 18, 12, 2, tzinfo=pacific)
        slot = due.astimezone(UTC).isoformat()
        now = due + timedelta(minutes=1)
        monkeypatch.setenv("HERMES_TIMEZONE", pacific.key)
        monkeypatch.setattr(J, "_hermes_now", lambda: now)
        stored = J.load_jobs()
        job = next(r for r in stored if r["id"] == job_id)
        job.update(
            schedule=J.parse_schedule("2 */6 * * *"),
            next_run_at=(due + timedelta(hours=6)).isoformat(),
            pending_slot={"scheduled_at": slot, "at": due.isoformat(), "by": "expired-owner"},
        )
        # Make the old owner provably expired without moving the occurrence out of grace.
        job["pending_slot"]["at"] = (due - timedelta(days=1)).isoformat()
        J.save_jobs(stored)

        proc = subprocess.run(
            [sys.executable, "-c", _REFUSE_AND_EXIT, str(REPO), now.isoformat()],
            cwd=str(REPO), capture_output=True, text=True, timeout=120, check=False,
        )
        assert proc.returncode == 137, proc.stderr[-2000:]
        refused = E.latest_execution(job_id)
        assert refused and "interpreter shutdown" in refused["error"]
        assert refused["scheduled_instant"] == slot and refused["started_at"] is None
        assert J.get_job(job_id)["pending_slot"]["scheduled_at"] == slot
        assert slot_env["fires"]() == 0

        # A fresh owner claims the exact occurrence after the second shutdown.
        S.tick(verbose=False, sync=True)
        assert slot_env["fires"]() == 1
        row = E.latest_execution(job_id)
        assert row["status"] == "completed" and row["scheduled_instant"] == slot
        S.tick(verbose=False, sync=True)
        assert slot_env["fires"]() == 1
        assert "pending_slot" not in J.get_job(job_id)

    @pytest.mark.parametrize("outbox_available", [True, False], ids=["available", "unavailable"])
    def test_expired_restored_slot_records_one_skip(self, slot_env, monkeypatch, caplog, outbox_available):
        from datetime import UTC, datetime
        from cron import delivery_queue, owner_alerts
        from hermes_cli.cron import _cron_doctor_issues_for_job

        J, E = slot_env["J"], slot_env["E"]
        job_id = slot_env["job_id"]
        now = datetime(2026, 11, 18, 22, 3, tzinfo=UTC)
        slot = (now - timedelta(hours=2)).isoformat()
        monkeypatch.setattr(J, "_hermes_now", lambda: now)
        stored = J.load_jobs()
        job = next(r for r in stored if r["id"] == job_id)
        job.update(catch_up=False, pending_slot={
            "scheduled_at": slot, "at": slot, "by": "expired-owner",
        })
        J.save_jobs(stored)
        key = f"missed-recurring:{job_id}:{slot}"

        def unavailable(*args):
            raise OSError("outbox unavailable")

        if not outbox_available:
            monkeypatch.setattr(owner_alerts, "queue_owner_failure", unavailable)

        assert J.get_due_jobs() == []
        assert J.get_due_jobs() == []
        saved = J.get_job(job_id)
        assert "pending_slot" not in saved
        next_run = datetime.fromisoformat(saved["next_run_at"])
        assert next_run > now
        assert saved["last_run_at"] is None and saved["last_status"] == "skipped"
        receipt = delivery_queue.get_status(key)
        if outbox_available:
            assert receipt is not None
            assert receipt["for_failure"] and receipt["status"] == "pending"
            assert slot in receipt["content"] and "catch-up is off" in receipt["content"]
        else:
            assert receipt is None
            assert "could not queue owner alert for skipped occurrence" in caplog.text
        assert any(slot in issue for issue in _cron_doctor_issues_for_job(saved))
        assert caplog.text.count("cron.occurrence_dropped") == 1
        row, = E.list_executions(job_id=job_id)
        assert row["status"] == "missed" and row["scheduled_instant"] == slot
        assert slot_env["fires"]() == 0

        monkeypatch.setattr(J, "_hermes_now", lambda: next_run)
        slot_env["S"].tick(verbose=False, sync=True)
        assert slot_env["fires"]() == 1

    def test_fired_slot_is_not_replayed_after_restart(self, slot_env):
        """Contract half two: a slot that DID run before the restart stays run."""
        S, J = slot_env["S"], slot_env["J"]
        S.tick(verbose=False, sync=True)
        assert slot_env["fires"]() == 1
        S._running_job_ids.clear()  # what a restart forgets
        S.tick(verbose=False, sync=True)
        assert slot_env["fires"]() == 1
        assert "pending_slot" not in J.get_job(slot_env["job_id"])
