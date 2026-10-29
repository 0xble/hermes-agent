"""A busy jobs lock is transient, not a broken disk or permission to lose a slot."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import os
from pathlib import Path
import select
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cron import jobs, store_health
from cron.scheduler_tick import _advance_or_drop_recurring, process_due_job


@contextmanager
def _holder(home):
    env = dict(os.environ, HERMES_HOME=str(home),
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    child = subprocess.Popen([sys.executable, "-c", """
import sys
from cron import jobs
with jobs._jobs_lock():
    print('ready', flush=True)
    sys.stdin.readline()
"""], env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert select.select([child.stdout], [], [], 10)[0], "holder did not acquire lock"
        assert child.stdout.readline().strip() == "ready"
        yield child.pid
    finally:
        stdout, stderr = child.communicate("release\n", timeout=10)
        assert child.returncode == 0, stdout + stderr


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("operation", ["scan", "advance", "tick", "claim", "repair"])
def test_contended_store_keeps_recurring_slot_without_unwritable_notice(
        tmp_path, monkeypatch, caplog, operation):
    from cron import scheduler, scheduler_tick

    home = tmp_path / "home"
    notices = Mock()
    monkeypatch.setattr(store_health, "_listener", notices)
    with jobs.use_cron_store(home):
        job = jobs.create_job(prompt="retain this slot", schedule="every 1h")
        slot = (jobs._hermes_now() - timedelta(seconds=5)).isoformat()
        jobs.update_job(job["id"], {"next_run_at": slot})
        due = jobs.get_due_jobs()
        assert [j["id"] for j in due] == [job["id"]]
        path = jobs._current_cron_store().jobs_file
        if operation == "repair":
            # A legacy bare list needs a save even for a read-only load.
            import json
            path.write_text(json.dumps(list(jobs.load_jobs())), encoding="utf-8")
        before = path.read_bytes()
        monkeypatch.setattr(jobs, "_JOBS_LOCK_TIMEOUT_SECONDS", 0)
        settled = Mock()
        monkeypatch.setattr(scheduler, "settle_unstarted_execution", settled)
        pool = Mock()
        if operation == "tick":
            monkeypatch.setattr(scheduler_tick, "_pre_dispatch_housekeeping", Mock())
            monkeypatch.setattr(scheduler, "_should_yield_tick_to_fresh_gateway", lambda: None)
            monkeypatch.setattr(scheduler, "_maybe_run_worktree_maintenance", Mock())
            monkeypatch.setattr(scheduler, "_sweep_stale_inflight_for_tick", Mock())
            monkeypatch.setattr(scheduler, "_sweep_mcp_orphans", Mock())
            monkeypatch.setattr(scheduler, "get_due_jobs", lambda: due)
            monkeypatch.setattr(scheduler, "_get_parallel_pool", pool)
        failure = None
        with _holder(home) as pid:
            try:
                if operation == "scan":
                    assert jobs.get_due_jobs() == []  # no uncommitted dispatch stamps escape
                elif operation == "advance":
                    _advance_or_drop_recurring(SimpleNamespace(advance_next_runs=jobs.advance_next_runs), due)
                elif operation == "tick":
                    assert scheduler_tick._tick_admitted(verbose=False) == 0
                    pool.assert_not_called()
                elif operation == "claim":
                    assert process_due_job(dict(due[0], execution_id="unstarted"), None, None, False) is False
                else:
                    assert jobs.load_jobs()[0]["id"] == job["id"]
            except Exception as exc:
                failure = exc
            assert store_health.degraded_record() is None, "contention must not mark a healthy disk unwritable"
            notices.assert_not_called()
            assert path.read_bytes() == before, "contention must not consume or remove the recurring slot"
            assert f"holder pid={pid}" in caplog.text
            assert any(r.levelname == "WARNING" and f"holder pid={pid}" in r.message for r in caplog.records)
            if operation == "advance":
                assert isinstance(failure, jobs.JobsLockContended), "defer the entire tick, never filter out recurring jobs"
                assert [j["id"] for j in due] == [job["id"]]
            else:
                assert failure is None
            if operation == "claim":
                settled.assert_called_once()
                assert "unwritable" not in settled.call_args.args[2].lower()
        # Releasing the actual cross-process lock restores normal dispatch on the next tick.
        assert [j["id"] for j in jobs.get_due_jobs()] == [job["id"]]
        assert jobs.mark_job_run(job["id"], True)
        current = jobs.get_job(job["id"])
        assert current["last_run_at"] is not None
        assert current["last_status"] == "ok"
        notices.assert_not_called()


@pytest.mark.platforms("posix")
def test_completed_run_bookkeeping_can_be_retried_after_contention(tmp_path, monkeypatch):
    from cron import executions

    home = tmp_path / "home"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")
    with jobs.use_cron_store(home):
        job = jobs.create_job(prompt="completed work", schedule="every 1h")
        assert jobs.claim_job_for_fire(job["id"], force=True)
        execution = executions.create_execution(job["id"], source="builtin")
        executions.finish_execution(execution["id"], success=True)
        assert jobs.get_job(job["id"])["last_run_at"] is None
        path = jobs._current_cron_store().jobs_file
        before = path.read_bytes()
        monkeypatch.setattr(jobs, "_JOBS_LOCK_TIMEOUT_SECONDS", 0)
        with _holder(home):
            with pytest.raises(RuntimeError, match="Refusing cron write") as refused:
                jobs.mark_job_run(job["id"], True, execution_id=execution["id"])
            assert not isinstance(refused.value, OSError)
            assert path.read_bytes() == before
        assert jobs.mark_job_run(job["id"], True, execution_id=execution["id"])
        current = jobs.get_job(job["id"])
        assert current is not None
        assert current["last_run_at"] is not None
        assert current["last_status"] == "ok"
        assert not current.get("fire_claim")
        assert executions.get_execution(execution["id"])["status"] == "completed"


@pytest.mark.platforms("posix")
def test_oneshot_refused_fire_claim_retries_next_locked_scan(tmp_path, monkeypatch):
    from cron import scheduler

    home = tmp_path / "home"
    monkeypatch.setattr(scheduler, "settle_unstarted_execution", Mock())
    with jobs.use_cron_store(home):
        job = jobs.create_job(prompt="retry unstarted one-shot", schedule="in 1m")
        slot = (jobs._hermes_now() - timedelta(seconds=5)).isoformat()
        jobs.update_job(job["id"], {"next_run_at": slot})
        due = jobs.get_due_jobs()
        assert [j["id"] for j in due] == [job["id"]]
        claim = jobs.get_job(job["id"])["run_claim"]
        assert jobs._claim_is_live(claim, jobs._hermes_now(), 1800)
        monkeypatch.setattr(jobs, "_JOBS_LOCK_TIMEOUT_SECONDS", 0)
        with _holder(home):
            assert not process_due_job(dict(due[0], execution_id="unstarted"), None, None, False)
            assert jobs.get_job(job["id"])["run_claim"] == claim
        # Queued/running workers and foreign claims still exclude another dispatch.
        monkeypatch.setattr(jobs, "_job_running_in_this_process", lambda _: True)
        assert jobs.get_due_jobs() == []
        monkeypatch.setattr(jobs, "_job_running_in_this_process", lambda _: False)
        jobs.update_job(job["id"], {"run_claim": dict(claim, by="foreign-host:123")})
        assert jobs.get_due_jobs() == []
        jobs.update_job(job["id"], {"run_claim": claim})
        retry = jobs.get_due_jobs()
        assert [j["id"] for j in retry] == [job["id"]]
        assert retry[0]["next_run_at"] == slot
        assert jobs.claim_job_for_fire(job["id"])
