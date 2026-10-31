"""Regression tests for #60703 — cron ticker silently stalls after gateway restart.

Three fixes under test:

1. ``_jobs_lock()`` bounds its cross-process flock: when another process holds
   ``.jobs.lock`` indefinitely, acquisition times out and logs the holder. Reads
   remain available, but writes are refused without cross-process ownership,
   instead of blocking the calling thread (and the cron ticker heartbeat) forever.

2. Claim freshness checks are bounded on both sides (``0 <= age < ttl``): a
   ``fire_claim``/``run_claim`` stamped in the FUTURE (clock/TZ skew across a
   restart) is treated as stale/overwritable, not eternally fresh.

3. ``_execute_job_now`` no longer mislabels paused/disabled/missing jobs as
   "already being fired".
"""

import os
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

import cron.jobs as jobs_mod
from cron.jobs import (
    _jobs_lock,
    claim_job_for_fire,
    create_job,
    get_job,
    load_jobs,
    save_jobs,
)


try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None


pytestmark = pytest.mark.skipif(fcntl is None, reason="flock semantics are POSIX-only")


def _hold_jobs_flock(path: Path, release: threading.Event, held: threading.Event):
    """Hold an exclusive flock on *path* from a separate fd until released.

    flock locks are per-open-file-description, so a second open() in the SAME
    process contends exactly like another process would.
    """
    fd = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        from cron.lock_owner import write_owner

        write_owner(fd, "_hold_jobs_flock")
        held.set()
        release.wait(timeout=30)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()


class TestBoundedJobsLock:
    def test_lock_acquisition_times_out_and_refuses_writes(self, monkeypatch, caplog):
        """A foreign holder of .jobs.lock must NOT block _jobs_lock forever."""
        job = create_job(name="held store", schedule="0 7 * * *", prompt="unchanged")
        jobs_file = jobs_mod._current_cron_store().jobs_file
        before = jobs_file.read_bytes()
        lock_path = jobs_mod._jobs_lock_file()
        lock_path.touch()

        monkeypatch.setattr(jobs_mod, "_JOBS_LOCK_TIMEOUT_SECONDS", 1.0)

        release = threading.Event()
        held = threading.Event()
        holder = threading.Thread(
            target=_hold_jobs_flock, args=(lock_path, release, held), daemon=True
        )
        holder.start()
        assert held.wait(timeout=10), "test holder failed to take the flock"

        try:
            start = time.monotonic()
            entered = False
            with caplog.at_level("WARNING", logger="cron.jobs"):
                with _jobs_lock():
                    entered = True
                    records = load_jobs()
                    assert records[0]["id"] == job["id"], "reads remain available under contention"
                    records[0]["prompt"] = "must not be persisted"
                    with pytest.raises(jobs_mod.JobsLockContended, match="Refusing cron write"):
                        save_jobs(records)
            elapsed = time.monotonic() - start

            assert entered, "read-only critical section must remain available"
            assert elapsed < 10, f"lock wait was not bounded (took {elapsed:.1f}s)"
            assert holder.is_alive(), "waiter must finish before the holder releases the lock"
            assert jobs_file.read_bytes() == before, "refused write must not change the store"
            timeout = next(r for r in caplog.records if "Timed out" in r.message)
            assert timeout.levelname == "WARNING"
            assert f"holder pid={os.getpid()}" in timeout.message
            assert "command=" in timeout.message and "held=" in timeout.message
            assert "operation='_hold_jobs_flock'" in timeout.message
            assert "writes are refused" in timeout.message
        finally:
            release.set()
            holder.join(timeout=10)


    def test_reentrant_nesting_still_works(self):
        with _jobs_lock():
            with _jobs_lock():  # must not deadlock or re-flock
                pass


class TestFutureDatedClaims:
    def _make_job(self, **kw):
        return create_job(name="claim job", schedule="0 7 * * *", prompt="x", **kw)

    def test_future_fire_claim_is_treated_as_stale(self):
        """A fire_claim stamped in the future must not block claiming forever."""
        job = self._make_job()
        jobs = load_jobs()
        for j in jobs:
            if j["id"] == job["id"]:
                future = jobs_mod._hermes_now() + timedelta(hours=6)
                j["fire_claim"] = {"at": future.isoformat(), "by": "other-host:1"}
        save_jobs(jobs)

        assert claim_job_for_fire(job["id"]) is True, (
            "future-dated claim must be overwritable, not eternally fresh"
        )

    def test_fresh_past_fire_claim_still_blocks(self):
        job = self._make_job()
        assert claim_job_for_fire(job["id"]) is True
        # Immediately re-claiming must be refused — claim is genuinely fresh.
        assert claim_job_for_fire(job["id"]) is False

    def test_expired_fire_claim_is_reclaimable(self):
        job = self._make_job()
        jobs = load_jobs()
        for j in jobs:
            if j["id"] == job["id"]:
                past = jobs_mod._hermes_now() - timedelta(hours=6)
                j["fire_claim"] = {"at": past.isoformat(), "by": "other-host:1"}
        save_jobs(jobs)
        assert claim_job_for_fire(job["id"]) is True


class TestHonestRunSkipMessages:
    def test_paused_job_not_reported_as_already_firing(self):
        from tools.cronjob_tools import _execute_job_now

        job = create_job(name="paused job", schedule="0 7 * * *", prompt="x")
        from cron.jobs import pause_job

        pause_job(job["id"])
        res = _execute_job_now(get_job(job["id"]))
        assert res["claimed"] is False
        assert "paused" in (res["error"] or "").lower()
        assert "already being fired" not in (res["error"] or "").lower()

    def test_missing_job_not_reported_as_already_firing(self):
        from tools.cronjob_tools import _execute_job_now

        res = _execute_job_now({"id": "does-not-exist-123"})
        assert res["claimed"] is False
        assert "no longer exists" in (res["error"] or "").lower()
