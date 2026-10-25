"""Manual completion follows the exact execution, never the job's previous run."""

import json
import queue
import threading

import pytest

from tests.waiting import wait_until


@pytest.mark.parametrize("terminal", ["completed", "failed", "unknown"])
def test_live_manual_run_notifies_once_after_exact_terminal_result(monkeypatch, tmp_path, terminal):
    from cron import executions, scheduler
    from tools import async_delegation as ad, cronjob_tools as tools
    from tools.process_registry import process_registry

    ad._reset_for_tests()
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    monkeypatch.setattr(ad, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(tools, "_background_session_key", lambda _: "manual-completion-owner")
    monkeypatch.setattr(tools, "get_job", lambda _: {
        "last_status": "error", "last_error": "previous config error",
        "last_delivery_error": "previous delivery error",
    })
    monkeypatch.setattr(tools, "claim_job_for_fire", lambda jid, **_: {
        "id": jid, "name": "manual", "deliver": "local", "fire_claim": {"by": "manual-owner"},
    })
    monkeypatch.setattr(tools, "_latest_job_output_excerpt", lambda _: "previous config error")
    events = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", events)
    original_get = executions.get_execution
    observed = []
    live_reads = 0
    execution_id = None
    pending = threading.Event()
    release_observer = threading.Event()

    def read_execution(eid):
        nonlocal live_reads
        row = original_get(eid)
        if row and row["status"] == "running":
            live_reads += 1
            if live_reads == 2:
                pending.set()
                assert release_observer.wait(10), "test did not release the live observer"
        observed.append(row["status"] if row else None)
        return row

    def bounded_waiter(job, **_):
        nonlocal execution_id
        execution_id = job["execution_id"]
        executions.mark_execution_running(execution_id)
        # Exercise the scheduler's real live-worker deferral branch.
        raise scheduler._ExternalWorkerPostHandoffError("bounded exit wait", worker_exited=False)

    monkeypatch.setattr(executions, "get_execution", read_execution)
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", bounded_waiter)
    try:
        result = tools._try_dispatch_background_run({"id": "manual-job", "name": "manual"})
        assert result["dispatched"]
        assert pending.wait(10), "observer did not stay pending for the live execution"
        assert execution_id is not None
        assert events.empty()
        durable = ad.get_durable_delegation(result["delegation_id"])
        assert durable is not None and durable["state"] == "running"
        if terminal == "unknown":
            monkeypatch.setattr(executions, "_PROCESS_ID", "replacement")
            monkeypatch.setattr(executions, "_owner_identity", lambda *_: "dead")
            assert executions.recover_interrupted_executions() == 1
        else:
            executions.finish_execution(
                execution_id, success=terminal == "completed",
                error="current worker failed" if terminal == "failed" else None,
                output="current execution output",
            )
        release_observer.set()
        assert wait_until(lambda: not events.empty(), timeout=10)
        event = events.get_nowait()
        assert original_get(execution_id)["status"] == terminal
        assert event["status"] == {"completed": "completed", "failed": "error", "unknown": "unknown"}[terminal]
        assert event["delegation_id"] == result["delegation_id"]
        assert "previous" not in event["summary"]
        if terminal != "unknown":
            assert "current execution output" in event["summary"]
        if terminal == "failed":
            assert event["error"] == "current worker failed"
        elif terminal == "completed":
            assert event["error"] is None
        assert observed[:2] == ["running", "running"]
        assert ad.recover_abandoned_delegations() == 0
        assert wait_until(lambda: ad.active_count() == 0, timeout=5)
        assert events.empty()
        durable = ad.get_durable_delegation(result["delegation_id"])
        assert durable["state"] == event["status"]
    finally:
        release_observer.set()
        # Release the adapter even if an assertion fails during development.
        if execution_id and original_get(execution_id)["status"] == "running":
            executions.finish_execution(execution_id, success=False, error="test cleanup")
        ad._reset_for_tests()


@pytest.mark.parametrize("state", ["completed", "failed", "unknown", "missing"])
def test_exact_completion_never_borrows_previous_output_or_delivery_error(monkeypatch, tmp_path, state):
    from cron import executions
    from tools import cronjob_tools as tools

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    monkeypatch.setattr(tools, "get_job", lambda _: {
        "last_status": "error", "last_error": "previous config error",
        "last_delivery_error": "previous delivery error",
    })
    monkeypatch.setattr(tools, "_latest_job_output_excerpt", lambda _: "previous config error")
    if state == "missing":
        eid = "missing-execution"
    else:
        run = executions.create_execution("manual-job", source="manual")
        eid = run["id"]
        if state == "unknown":
            monkeypatch.setattr(executions, "_PROCESS_ID", "replacement")
            monkeypatch.setattr(executions, "_owner_identity", lambda *_: "dead")
            executions.recover_interrupted_executions()
        else:
            executions.finish_execution(eid, success=state == "completed", error="current failure")
    result = tools._manual_run_completion(
        {"success": True, "error": "previous waiter error"},
        "manual-job", "manual", "telegram:123", 0, execution_id=eid,
    )
    assert "previous" not in json.dumps(result)
    assert "--- JOB OUTPUT ---" not in result["summary"]
    assert result["status"] == {"completed": "completed", "failed": "error", "unknown": "unknown", "missing": "unknown"}[state]
