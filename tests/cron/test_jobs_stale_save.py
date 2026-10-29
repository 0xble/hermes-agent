"""A timed-out writer must not undo a successful edit from another process."""
from __future__ import annotations

import os
import select
from pathlib import Path
import subprocess
import sys

import pytest

from cron import jobs


@pytest.mark.skipif(jobs.fcntl is None, reason="POSIX flock required")
@pytest.mark.parametrize("nested", [False, True])
def test_stale_writer_cannot_save_after_real_lock_timeout(tmp_path, monkeypatch, nested):
    home = tmp_path / "home"
    env = dict(os.environ, HERMES_HOME=str(home),
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    with jobs.use_cron_store(home):
        job = jobs.create_job(prompt="original", schedule="every 1h", deliver="local")
        stale = jobs.load_jobs()  # Writer A, before B's acknowledged edit.
        child = subprocess.Popen(
            [sys.executable, "-c", """
import sys
from cron import jobs
with jobs._jobs_lock():
    assert jobs.update_job(sys.argv[1], {"deliver": "telegram:edited"}) is not None
    print("edited", flush=True)
    sys.stdin.readline()
""", job["id"]], env=env, text=True, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert select.select([child.stdout], [], [], 10)[0], "editor did not become ready"
            assert child.stdout.readline().strip() == "edited"
            assert jobs.get_job(job["id"])["deliver"] == "telegram:edited"
            monkeypatch.setattr(jobs, "_JOBS_LOCK_TIMEOUT_SECONDS", 0)
            stale[0]["last_run_at"] = "2026-10-27T20:48:00+00:00"
            refused = False
            try:
                if nested:
                    with jobs._jobs_lock():
                        jobs.save_jobs(stale)
                else:
                    jobs.save_jobs(stale)
            except jobs.JobsLockContended:
                refused = True
            # Assert the value first: the unfixed implementation actually reverts it.
            assert jobs.get_job(job["id"])["deliver"] == "telegram:edited"
            assert refused, "a write without cross-process ownership must fail loudly"
        finally:
            stdout, stderr = child.communicate("release\n", timeout=10)
        assert child.returncode == 0, stdout + stderr
        # A healthy retry re-reads the store and changes only scheduler outcome fields.
        assert jobs.mark_job_run(job["id"], True)
        current = jobs.get_job(job["id"])
        assert current["deliver"] == "telegram:edited"
        assert current["last_status"] == "ok"


@pytest.mark.skipif(jobs.fcntl is None, reason="POSIX flock required")
def test_worker_completion_cannot_overwrite_concurrent_editor(tmp_path):
    """Interleave the real worker completion load/save around the CLI's locked edit."""
    home = tmp_path / "home"
    env = dict(os.environ, HERMES_HOME=str(home),
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    with jobs.use_cron_store(home):
        job = jobs.create_job(prompt="original", schedule="every 1h", deliver="local")
        # B owns the cross-process lock. A degrades, loads, then waits inside mark_job_run.
        with jobs._jobs_lock():
            child = subprocess.Popen([sys.executable, "-c", """
import sys
from cron import jobs
jobs._JOBS_LOCK_TIMEOUT_SECONDS = 0
record = jobs._record_run_outcome
def interleave(*args, **kwargs):
    print("loaded", flush=True)
    sys.stdin.readline()
    return record(*args, **kwargs)
jobs._record_run_outcome = interleave
try:
    jobs.mark_job_run(sys.argv[1], True)
except jobs.JobsLockContended:
    print("refused", flush=True)
else:
    print("saved", flush=True)
""", job["id"]], env=env, text=True, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                assert select.select([child.stdout], [], [], 10)[0], "worker did not load"
                assert child.stdout.readline().strip() == "loaded"
                assert jobs.update_job(job["id"], {"deliver": "telegram:edited"})
                stdout, stderr = child.communicate("finish\n", timeout=10)
                assert jobs.get_job(job["id"])["deliver"] == "telegram:edited"
                assert stdout.strip() == "refused", stderr
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert jobs.mark_job_run(job["id"], True)
        assert jobs.get_job(job["id"])["deliver"] == "telegram:edited"


@pytest.mark.parametrize("field,value", [
    ("deliver", "telegram:edited"), ("enabled", False), ("state", "paused"),
    ("paused_at", "2026-10-27T20:47:00Z"),
    ("prompt", "edited prompt"), ("schedule", {"kind": "interval", "minutes": 120}),
])
def test_stale_bookkeeping_merges_only_its_changed_fields(field, value):
    job = jobs.create_job(prompt="original", schedule="every 1h", deliver="local")
    stale = jobs.load_jobs()
    with jobs._jobs_lock():
        edited = jobs.load_jobs()
        edited[0][field] = value
        jobs.save_jobs(edited)
    stale[0]["last_run_at"] = "2026-10-27T20:48:00+00:00"
    stale[0]["last_status"] = "ok"
    jobs.save_jobs(stale)
    # Inspect persisted state, not get_job's derived effective state for half-paused records.
    current = next(j for j in jobs.load_jobs() if j["id"] == job["id"])
    assert current[field] == value
    assert current["last_status"] == "ok"
    assert current["last_run_at"] == stale[0]["last_run_at"]


def test_reused_snapshot_advances_only_its_own_baseline():
    job = jobs.create_job(prompt="original", schedule="every 1h", deliver="local")
    snapshot = jobs.load_jobs()
    assert jobs.update_job(job["id"], {"deliver": "telegram:newer"})
    for status in ("ok", "error"):
        snapshot[0]["last_status"] = status
        jobs.save_jobs(snapshot)
        current = jobs.get_job(job["id"])
        assert current["deliver"] == "telegram:newer"
        assert current["last_status"] == status


def test_conflicting_stale_field_change_is_refused():
    job = jobs.create_job(prompt="original", schedule="every 1h", deliver="local")
    stale = jobs.load_jobs()
    assert jobs.update_job(job["id"], {"deliver": "telegram:newer"})
    stale[0]["deliver"] = "telegram:stale"
    with pytest.raises(RuntimeError, match="changed since load"):
        jobs.save_jobs(stale)
    assert jobs.get_job(job["id"])["deliver"] == "telegram:newer"


def test_stale_bookkeeping_does_not_resurrect_removed_job():
    job = jobs.create_job(prompt="original", schedule="every 1h")
    stale = jobs.load_jobs()
    assert jobs.remove_job(job["id"])
    stale[0]["last_status"] = "ok"
    with pytest.raises(RuntimeError, match="changed since load"):
        jobs.save_jobs(stale)
    assert jobs.get_job(job["id"]) is None


@pytest.mark.parametrize("result", [False, None, OSError("lock unavailable")])
def test_all_save_entrypoints_refuse_unowned_lock(monkeypatch, result):
    job = jobs.create_job(prompt="original", schedule="every 1h")
    path = jobs._current_cron_store().jobs_file
    before = path.read_bytes()

    def unavailable(*_args):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(jobs, "_acquire_flock", unavailable)
    for save in (
        lambda: jobs.save_jobs([], replace=True),
        lambda: jobs.save_jobs_document({"jobs": []}),
        lambda: jobs.update_job(job["id"], {"prompt": "changed"}),
    ):
        with pytest.raises(jobs.JobsLockContended, match="Refusing cron write"):
            save()
        assert path.read_bytes() == before
    # Read-only degradation remains available.
    with jobs._jobs_lock():
        assert jobs.get_job(job["id"])["prompt"] == "original"
