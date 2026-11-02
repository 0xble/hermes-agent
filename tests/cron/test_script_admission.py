"""Script-only cron fires must not queue behind detached agent waiters."""

import subprocess
import sys
import threading
from datetime import UTC, datetime

import pytest

from cron import executions, jobs, scheduler
from cron import scheduler_admission as admission
from cron.scheduler_provider import _profile_cron_scope
from hermes_constants import hermes_home_key
from tests.waiting import wait_until


@pytest.fixture
def home(tmp_path, monkeypatch):
    now = datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    monkeypatch.setattr(executions, "_hermes_now", lambda: now)
    monkeypatch.setattr(scheduler, "_should_yield_tick_to_fresh_gateway", lambda: None)
    monkeypatch.setattr(scheduler, "_sweep_stale_inflight_for_tick", lambda *args: None)
    monkeypatch.setattr(scheduler, "_sweep_mcp_orphans", lambda: None)
    monkeypatch.setattr(scheduler, "_maybe_run_worktree_maintenance", lambda: None)
    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {}})
    monkeypatch.setattr(scheduler, "cron_env_setting", lambda name: "")
    admission._wake.clear()
    with _profile_cron_scope(tmp_path):
        yield tmp_path, now
        scheduler._shutdown_parallel_pool()
        admission._reserved_claims.clear()
        admission._loaded_protected.clear()
        admission._wake.clear()


def due_job(now, **kwargs):
    if kwargs.get("no_agent") and "script" not in kwargs:
        script = scheduler._get_hermes_home() / "scripts" / "noop.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("pass\n")
        kwargs["script"] = str(script)
    job = jobs.create_job(prompt="scheduled work", schedule=kwargs.pop("schedule", "every 10m"),
                          deliver="local", **kwargs)
    jobs.update_job(job["id"], {"next_run_at": now.isoformat()})
    persisted = jobs.get_job(job["id"])
    assert persisted is not None
    return persisted


def test_due_script_runs_while_general_and_protected_detached_workers_wait(home, monkeypatch):
    path, now = home
    # Four active general waiters, one queued agent and the existing protected reservation.
    general = [due_job(now) for _ in range(5)]
    protected = due_job(now, schedule="every 2m")
    processes = []
    started = set()
    lock = threading.Lock()
    script_done = threading.Event()

    def run(item, **kwargs):
        executions.mark_execution_running(item["execution_id"])
        if item.get("no_agent"):
            success, _, _, error = scheduler.run_job(item)
            executions.finish_execution(item["execution_id"], success=success, error=error)
            jobs.mark_job_run(item["id"], success, error)
            script_done.set()
            return success
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                   stdin=subprocess.PIPE)
        with scheduler._running_lock:
            scheduler._external_worker_modes[scheduler._inflight_key(item["id"])] = "detached"
        with lock:
            processes.append(process)
            started.add(item["id"])
        executions.finish_execution(item["execution_id"], success=True)
        return scheduler._wait_for_external_cron_worker(
            process, execution_id=item["execution_id"], job_id=item["id"],
            terminal_exit_grace_seconds=30)

    monkeypatch.setattr(scheduler, "run_one_job", run)
    try:
        assert scheduler.tick(sync=False) == 6
        assert wait_until(lambda: len(started) == 5, timeout=5)
        assert protected["id"] in started
        assert general[4]["id"] not in started
        scripts = path / "scripts"
        scripts.mkdir(exist_ok=True)
        marker = path / "script-started"
        script = scripts / "wake.py"
        script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('awake')\n")
        wake = due_job(now, no_agent=True, script=str(script))
        assert scheduler.tick(sync=False) == 1
        assert script_done.wait(5), "due no_agent fire queued behind detached agent waiters"
        assert marker.read_text() == "awake"
        row = executions.latest_execution(wake["id"])
        assert row is not None
        assert row["status"] == "completed"
        assert row["started_at"] == now.isoformat()
        assert len(started) == 5
        assert all(process.poll() is None for process in processes)
    finally:
        # The queued fifth agent can start as slots return, so let it exit immediately too.
        monkeypatch.setattr(scheduler, "run_one_job", lambda item, **kwargs: True)
        for process in processes:
            process.communicate(timeout=10)


def test_script_fifo_is_serial_scoped_and_does_not_take_protected_reservation(home, tmp_path):
    path, now = home
    release, first_started, protected_started = (threading.Event() for _ in range(3))
    starts = []
    first, second = due_job(now, no_agent=True), due_job(now, no_agent=True)
    protected = due_job(now, schedule="every 2m")

    def body(item):
        starts.append((item["id"], hermes_home_key(scheduler._get_hermes_home())))
        if item["id"] == first["id"]:
            first_started.set()
            assert release.wait(10)
        if item["id"] == protected["id"]:
            protected_started.set()
        return True

    futures = []
    try:
        pool = scheduler._get_parallel_pool(4)
        futures += admission.submit_due_jobs([first], pool, body)
        assert first_started.wait(5)
        futures += admission.submit_due_jobs([second, protected], pool, body)
        assert protected_started.wait(5)
        assert second["id"] not in [job_id for job_id, _ in starts]
        # A blocked script in A cannot consume B's script capacity or release its claim.
        other_home = tmp_path / "other"
        with _profile_cron_scope(other_home):
            other = due_job(now, no_agent=True)
            other_futures = admission.submit_due_jobs([other], scheduler._get_parallel_pool(4), body)
            assert other_futures[0].result(timeout=5) is True
            assert starts[-1] == (other["id"], hermes_home_key(other_home))
        assert scheduler.is_job_running(first["id"])
    finally:
        release.set()
        for future in futures:
            assert future.result(timeout=5) is True
    local = [job_id for job_id, key in starts if key == hermes_home_key(path)]
    assert local.index(first["id"]) < local.index(second["id"])
    assert not scheduler.is_job_running(first["id"])
    assert not scheduler.is_job_running(second["id"])
    scheduler.discard_parallel_pools([hermes_home_key(other_home)])
