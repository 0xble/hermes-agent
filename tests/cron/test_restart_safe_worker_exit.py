"""Tests for restart-safe cron worker exit proof and outcome classification."""

from contextlib import nullcontext
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest


def test_terminal_worker_exit_wait_is_bounded(monkeypatch):
    """A terminal row must not wedge the gateway if the worker ignores teardown."""
    from cron import scheduler

    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired(cmd="worker", timeout=0.01)
    monkeypatch.setattr(scheduler, "get_execution", lambda _execution_id: {"id": "exec-1", "status": "completed"})
    with pytest.raises(RuntimeError, match="stayed alive after its terminal exit grace period"):
        scheduler._wait_for_external_cron_worker_body(process, execution_id="exec-1", wait_for_process_exit=True, terminal_exit_grace_seconds=0.01)
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()


def test_terminal_worker_termination_is_reported_as_unknown(monkeypatch):
    """A successful parent-side terminate must not be projected as a clean success."""
    from cron import scheduler

    state = {"returncode": None}
    process = Mock()
    process.poll.side_effect = lambda: state["returncode"]

    def wait(timeout=None):
        if state["returncode"] is None:
            raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout or 0.0)
        return state["returncode"]

    process.wait.side_effect = wait
    process.terminate.side_effect = lambda: state.update(returncode=-15)
    monkeypatch.setattr(scheduler, "get_execution", lambda _execution_id: {"id": "exec-1", "status": "completed"})
    with pytest.raises(scheduler._ExternalWorkerExitTimeout, match="was terminated") as raised:
        scheduler._wait_for_external_cron_worker_body(process, execution_id="exec-1", wait_for_process_exit=True, terminal_exit_grace_seconds=0.01)
    assert raised.value.worker_terminated is True
    process.terminate.assert_called_once_with()
    process.kill.assert_not_called()


def test_terminal_worker_natural_exit_during_stop_is_not_marked_terminated(monkeypatch):
    """A stop race that observes a normal exit must preserve the worker result."""
    from cron import scheduler

    state = {"returncode": None}
    process = Mock()
    process.poll.side_effect = lambda: state["returncode"]

    def wait(timeout=None):
        if state["returncode"] is None:
            raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout or 0.0)
        return state["returncode"]

    process.wait.side_effect = wait
    process.terminate.side_effect = lambda: state.update(returncode=0)
    monkeypatch.setattr(scheduler, "get_execution", lambda _execution_id: {"id": "exec-1", "status": "completed"})
    assert scheduler._wait_for_external_cron_worker_body(process, execution_id="exec-1", wait_for_process_exit=True, terminal_exit_grace_seconds=0.01) is True
    process.terminate.assert_called_once_with()
    process.kill.assert_not_called()


def test_terminal_worker_nonzero_exit_is_not_projected_as_success(monkeypatch):
    """A nonzero worker exit after commit must reach the failure projection lane."""
    from cron import scheduler

    process = Mock()
    process.poll.return_value = 1
    process.wait.return_value = 1
    monkeypatch.setattr(scheduler, "get_execution", lambda _execution_id: {"id": "exec-1", "status": "completed"})
    with pytest.raises(scheduler._ExternalWorkerExitTimeout, match="status 1") as raised:
        scheduler._wait_for_external_cron_worker_body(process, execution_id="exec-1", wait_for_process_exit=True)
    assert raised.value.worker_terminated is False
    assert raised.value.worker_exit_code == 1


def test_terminal_worker_within_hard_wall_budget_is_not_killed(monkeypatch):
    """Delivery may outlive the fixed grace while the worker still owns its lifetime budget."""
    from cron import scheduler

    monkeypatch.setattr(scheduler, "get_execution", lambda _execution_id: {"id": "exec-1", "status": "completed"})
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.0)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert scheduler._wait_for_external_cron_worker_body(process, execution_id="exec-1", wait_for_process_exit=True, terminal_exit_grace_seconds=0.01, worker_hard_wall_deadline=time.monotonic() + 1.5) is True
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_terminated_terminal_worker_is_not_projected_ok(monkeypatch):
    """A completed ledger row is still an unknown delivery when the parent killed its worker."""
    from cron import scheduler
    from cron.scheduler_worker_failure import record_external_worker_outcome

    job = {"id": "job-1", "execution_id": "exec-1", "fire_claim": {"by": "owner-1"}}
    mark_job_run = Mock()
    deliver_crash = Mock(return_value=("notice delivery failed", None))
    monkeypatch.setattr(scheduler, "get_execution", lambda _execution_id: {"id": "exec-1", "status": "completed", "output": "ok"})
    monkeypatch.setattr(scheduler, "fire_claim_fence", lambda *args, **kwargs: nullcontext(True))
    monkeypatch.setattr(scheduler, "mark_job_run", mark_job_run)
    monkeypatch.setattr(scheduler, "_install_fire_secret_scope", lambda: object())
    monkeypatch.setattr(scheduler, "_reset_fire_secret_scope", lambda _tokens: None)
    monkeypatch.setattr(scheduler, "_deliver_crash_failure", deliver_crash)
    assert record_external_worker_outcome(job, error="Restart-safe cron worker was terminated after its terminal exit grace period", worker_exited=True, worker_terminated=True) is True
    assert mark_job_run.call_args.kwargs["success"] is False
    assert mark_job_run.call_args.kwargs["error"].startswith("Restart-safe cron worker was terminated")
    deliver_crash.assert_called_once()


def test_nonzero_terminal_worker_projects_post_commit_failure(monkeypatch):
    """A completed execution with a failed worker must not clear the job as successful."""
    from cron import scheduler
    from cron.scheduler_worker_failure import record_external_worker_outcome

    job = {"id": "job-1", "execution_id": "exec-1", "fire_claim": {"by": "owner-1"}}
    mark_job_run = Mock()
    deliver_crash = Mock(return_value=(None, "delivered"))
    monkeypatch.setattr(scheduler, "get_execution", lambda _execution_id: {"id": "exec-1", "status": "completed", "output": "ok"})
    monkeypatch.setattr(scheduler, "fire_claim_fence", lambda *args, **kwargs: nullcontext(True))
    monkeypatch.setattr(scheduler, "mark_job_run", mark_job_run)
    monkeypatch.setattr(scheduler, "_install_fire_secret_scope", lambda: object())
    monkeypatch.setattr(scheduler, "_reset_fire_secret_scope", lambda _tokens: None)
    monkeypatch.setattr(scheduler, "_deliver_crash_failure", deliver_crash)
    assert record_external_worker_outcome(job, error="Restart-safe cron worker exited with status 1 after terminalizing its execution.", worker_exited=True, worker_exit_code=1) is True
    assert mark_job_run.call_args.kwargs["success"] is False
    deliver_crash.assert_called_once()


def test_external_worker_outcome_uses_explicit_execution_id_for_reloaded_job(monkeypatch):
    """Deferred reconciliation must not depend on a transient job field."""
    from cron import scheduler
    from cron.scheduler_worker_failure import record_external_worker_outcome

    job = {"id": "job-1", "fire_claim": {"by": "owner-1"}}
    mark_job_run = Mock()
    monkeypatch.setattr(scheduler, "get_execution", lambda execution_id: {"id": execution_id, "status": "completed", "output": "ok"})
    monkeypatch.setattr(scheduler, "fire_claim_fence", lambda *args, **kwargs: nullcontext(True))
    monkeypatch.setattr(scheduler, "mark_job_run", mark_job_run)
    assert record_external_worker_outcome(job, worker_exited=True, execution_id="exec-1") is True
    assert mark_job_run.call_args.kwargs["success"] is True


def test_deferred_outcome_uses_original_fire_owner(monkeypatch):
    """A reloaded job cannot let a newer fire claim the old worker's outcome."""
    from cron import scheduler
    from cron.scheduler_worker_failure import record_external_worker_outcome

    job = {"id": "job-1", "fire_claim": {"by": "new-owner"}}
    mark_job_run = Mock()
    deliver_crash = Mock()
    fence = Mock(return_value=nullcontext(False))
    monkeypatch.setattr(scheduler, "get_execution", lambda _execution_id: {"id": "exec-1", "status": "failed", "error": "worker failed"})
    monkeypatch.setattr(scheduler, "fire_claim_fence", fence)
    monkeypatch.setattr(scheduler, "mark_job_run", mark_job_run)
    monkeypatch.setattr(scheduler, "_deliver_crash_failure", deliver_crash)
    assert record_external_worker_outcome(job, error="stale worker result", worker_exited=True, execution_id="exec-1", expected_fire_owner="old-owner") is True
    assert fence.call_args.kwargs["expected_owner"] == "old-owner"
    mark_job_run.assert_not_called()
    deliver_crash.assert_not_called()


def test_deferred_worker_reaper_reconciles_outcome_before_release(monkeypatch, tmp_path):
    """A bounded early return still settles the durable job outcome after worker exit."""
    from cron import scheduler

    home = tmp_path / "profile"
    job_id = "job-deferred"
    key = scheduler._inflight_key(job_id, home)
    owner = object()
    process = Mock()
    state = {"returncode": None}
    process.poll.side_effect = lambda: state["returncode"]
    process.wait.side_effect = lambda timeout=None: state.update(returncode=-9) or state["returncode"]
    reconciled = threading.Event()
    reconciled_kwargs = {}
    released = threading.Event()
    def capture_reconciliation(*args, **kwargs):
        reconciled_kwargs.update(kwargs)
        reconciled.set()
    monkeypatch.setattr(scheduler, "_reconcile_deferred_external_worker_outcome", capture_reconciliation)
    monkeypatch.setattr(scheduler, "release_running_job", lambda *args, **kwargs: released.set())
    with scheduler._running_lock:
        scheduler._running_job_ids.add(key)
        scheduler._running_registration_owners[key] = owner
    try:
        scheduler._defer_external_worker_claim_release(job_id, home, owner, process, execution_id="exec-deferred", expected_fire_owner="old-owner", outcome_error="worker remained alive after bounded exit wait", worker_terminated=True)
        assert reconciled.wait(2.0)
        assert reconciled_kwargs["expected_fire_owner"] == "old-owner"
        assert released.wait(2.0)
    finally:
        with scheduler._running_lock:
            scheduler._running_job_ids.discard(key)
            scheduler._running_registration_owners.pop(key, None)
            scheduler._external_worker_claims.pop(key, None)
            scheduler._external_worker_reapers.clear()