"""A timed-out model worker retains durable ownership until its exact Future exits."""

import concurrent.futures
from datetime import datetime, timedelta, timezone
import contextvars
import threading
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("replace_owner", [False, True])
def test_lingering_worker_defers_durable_completion(
    tmp_path, monkeypatch, replace_owner
):
    import cron.scheduler as scheduler
    from cron import executions, jobs

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda _job: False)
    monkeypatch.setattr(scheduler, "_RUN_CLAIM_HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(scheduler, "_cron_inactivity_seconds", lambda: 0.01)
    monkeypatch.setattr(
        scheduler,
        "_load_cron_job_config",
        lambda *a: SimpleNamespace(cfg={}, model="fixture"),
    )
    monkeypatch.setattr(
        scheduler,
        "_resolve_cron_agent_setup",
        lambda *a: SimpleNamespace(blocked=None, model="fixture"),
    )
    monkeypatch.setattr(scheduler, "_open_cron_session_db", lambda _job: None)
    monkeypatch.setattr(
        scheduler, "_reload_dotenv_and_publish_delivery_target", lambda _job: None
    )
    monkeypatch.setattr(scheduler, "_teardown_cron_agent", lambda *a, **kw: None)

    started = threading.Event()
    release = threading.Event()
    interrupted = threading.Event()
    run_returned = threading.Event()
    finalizer_settled = threading.Event()
    runner_done = threading.Event()
    fire_renewed = threading.Event()
    run_renewed = threading.Event()
    result = []
    errors = []
    late_writes = []

    class Agent:
        def run_conversation(self, *a, **kw):
            started.set()
            release.wait()
            late_writes.append("worker exited")
            return {"final_response": "late success", "completed": True}

        def get_activity_summary(self):
            return {"seconds_since_activity": 999, "api_call_count": 1}

        def hard_interrupt(self, *a, **kw):
            interrupted.set()

    monkeypatch.setattr(scheduler, "_construct_cron_agent", lambda *a, **kw: Agent())

    def idle(**kwargs):
        assert started.wait(timeout=10)
        return True

    monkeypatch.setattr(scheduler, "_inactivity_watchdog_loop", idle)

    real_fire_heartbeat = scheduler.heartbeat_fire_claim
    real_run_heartbeat = scheduler.heartbeat_run_claim

    def observed_fire_heartbeat(*a, **kw):
        renewed = real_fire_heartbeat(*a, **kw)
        if renewed and run_returned.is_set():
            fire_renewed.set()
        return renewed

    def observed_run_heartbeat(*a, **kw):
        renewed = real_run_heartbeat(*a, **kw)
        if renewed and run_returned.is_set():
            run_renewed.set()
        return renewed

    monkeypatch.setattr(scheduler, "heartbeat_fire_claim", observed_fire_heartbeat)
    monkeypatch.setattr(scheduler, "heartbeat_run_claim", observed_run_heartbeat)

    real_run = scheduler.run_job

    def observed_run(*a, **kw):
        try:
            return real_run(*a, **kw)
        finally:
            run_returned.set()

    monkeypatch.setattr(scheduler, "run_job", observed_run)
    real_wait = concurrent.futures.wait

    def observed_wait(*a, **kw):
        # Observe a real finalizer wait without altering Future completion or
        # timeouts. Before the fix, terminal accounting completes instead.
        if threading.current_thread() is runner and run_returned.is_set():
            finalizer_settled.set()
        return real_wait(*a, **kw)

    monkeypatch.setattr(concurrent.futures, "wait", observed_wait)

    with jobs.use_cron_store(tmp_path):
        created = jobs.create_job(
            prompt="fixture",
            schedule=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            if replace_owner
            else "every 5m",
            deliver="local",
        )
        jobs.trigger_job(created["id"])
        due = jobs.get_due_jobs()
        assert len(due) == 1
        claimed = jobs.claim_job_for_fire(created["id"], return_job=True)
        assert isinstance(claimed, dict)
        owner = claimed["fire_claim"]["by"]
        run_claim = claimed.get("run_claim")
        if replace_owner:
            assert run_claim
        execution = executions.create_execution(created["id"], source="test")
        claimed["execution_id"] = execution["id"]

        def run():
            try:
                result.append(scheduler.run_one_job(claimed))
            except BaseException as exc:
                errors.append(exc)
            finally:
                runner_done.set()
                finalizer_settled.set()

        ctx = contextvars.copy_context()
        runner = threading.Thread(target=lambda: ctx.run(run), daemon=True)
        runner.start()
        try:
            assert run_returned.wait(timeout=25), errors
            assert interrupted.is_set()
            assert finalizer_settled.wait(timeout=10), errors
            current = jobs.get_job(created["id"])
            assert current["fire_claim"] and current["fire_claim"]["by"] == owner
            assert current.get("last_run_at") is None
            assert executions.get_execution(execution["id"])["status"] == "running"
            assert not runner_done.is_set()
            assert not late_writes
            # A separate durable claimant cannot acquire even with manual force.
            assert jobs.claim_job_for_fire(created["id"], force=True) is False
            assert fire_renewed.wait(timeout=5), "fire heartbeat stopped with run_job"
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; from cron import jobs; "
                    "ctx=jobs.use_cron_store(sys.argv[1]); ctx.__enter__(); "
                    "print(jobs.claim_job_for_fire(sys.argv[2], force=True))",
                    str(tmp_path),
                    created["id"],
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert probe.returncode == 0, probe.stderr
            assert probe.stdout.strip() == "False"
            if run_claim:
                assert current["run_claim"]["by"] == run_claim["by"]
                assert run_renewed.wait(timeout=5), (
                    "one-shot heartbeat stopped with run_job"
                )
            if replace_owner:
                replacement = {
                    "by": "replacement-owner",
                    "at": current["fire_claim"]["at"],
                }
                jobs.update_job(created["id"], {"fire_claim": replacement})
        finally:
            release.set()
            runner.join(timeout=10)
        assert not runner.is_alive()
        assert errors == []
        assert result == [True]
        current = jobs.get_job(created["id"])
        if replace_owner:
            assert current["fire_claim"]["by"] == "replacement-owner"
            assert current.get("last_run_at") is None
        else:
            assert current["fire_claim"] is None
            assert current["last_status"] != "ok"
            assert "TimeoutError" in current["last_error"]
        terminal = executions.get_execution(execution["id"])
        assert terminal["status"] != "running"
        assert "late success" not in str(terminal)
