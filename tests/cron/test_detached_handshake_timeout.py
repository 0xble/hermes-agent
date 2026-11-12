"""Detached gateway handshake timeouts defer an unstarted cron occurrence."""

from contextlib import contextmanager
import pytest


def test_attach_retries_connect_timeout_then_adopts(monkeypatch):
    from cron import detached_authority

    sleeps = []
    monkeypatch.setattr(detached_authority.time, "sleep", sleeps.append)
    calls = []

    class RPC:
        def __call__(self, method, **_params):
            calls.append(method)
            if len(calls) <= 3:
                raise TimeoutError("gateway handshake timed out (connect)")
            return {"owner_epoch": 7} if method == "worker.adopt" else None

    result = detached_authority._attach_worker(
        RPC(), {"execution_id": "e", "session_id": "s", "generation": 1}
    )

    assert result == {"owner_epoch": 7}
    assert calls == ["worker.register"] * 3 + ["worker.register", "worker.adopt"]
    assert sleeps == [0.1, 0.2, 0.4]


def test_exhausted_attach_timeout_is_phase_labeled():
    from cron import detached_authority

    class RPC:
        def __call__(self, _method, **_params):
            raise TimeoutError("gateway handshake timed out (connect)")

    with pytest.raises(detached_authority.DetachedHandshakeTimeout, match=r"\(connect\)"):
        detached_authority._attach_worker(
            RPC(), {"execution_id": "e", "session_id": "s", "generation": 1}
        )


def test_prestart_handshake_failure_defers_without_job_failure(monkeypatch):
    from cron import scheduler, scheduler_worker_failure

    job = {"id": "job", "fire_claim": {"by": "owner"}}
    execution = {
        "status": "failed",
        "error": "gateway handshake timed out (connect)",
        "job_id": "job",
    }
    calls = []

    @contextmanager
    def fence(*_args, **_kwargs):
        yield True

    monkeypatch.setattr(scheduler, "get_execution", lambda _execution_id: execution)
    monkeypatch.setattr(scheduler, "fire_claim_fence", fence)
    monkeypatch.setattr(
        scheduler,
        "defer_unstarted_detached_handshake",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda *_args, **_kwargs: pytest.fail("pre-start handshake must not mark a job failure"),
    )

    assert scheduler_worker_failure.record_external_worker_outcome(
        job, execution_id="execution", expected_fire_owner="owner", worker_exited=True
    ) is True
    assert calls and calls[0][0][2] == execution["error"]


def test_run_one_job_defers_marked_handshake_before_delivery(monkeypatch):
    from cron import scheduler

    finished = []
    deferred = []

    def run_job(job, **_kwargs):
        job["_detached_handshake_deferred"] = True
        job["_detached_handshake_error"] = "gateway handshake timed out (connect)"
        return False, "", "", job["_detached_handshake_error"]

    monkeypatch.setattr(scheduler, "create_execution", lambda *_a, **_kw: {"id": "execution"})
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda _execution_id: {})
    monkeypatch.setattr(scheduler, "run_job", run_job)
    monkeypatch.setattr(scheduler, "finish_execution", lambda *args, **kwargs: finished.append((args, kwargs)))
    monkeypatch.setattr(
        scheduler,
        "defer_unstarted_detached_handshake",
        lambda *args, **kwargs: deferred.append((args, kwargs)),
    )
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_a, **_kw: pytest.fail("must defer"))

    assert scheduler.run_one_job({"id": "job", "name": "job"}) is True
    assert deferred and deferred[0][0][2] == "gateway handshake timed out (connect)"
    assert not finished


def test_poststart_timeout_still_marks_failed(monkeypatch):
    from cron import scheduler, scheduler_worker_failure

    job = {"id": "job", "fire_claim": {"by": "owner"}}
    execution = {"status": "failed", "error": "Script timed out after 30s", "job_id": "job"}
    marked = []

    @contextmanager
    def fence(*_args, **_kwargs):
        yield True

    monkeypatch.setattr(scheduler, "get_execution", lambda _execution_id: execution)
    monkeypatch.setattr(scheduler, "fire_claim_fence", fence)
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda *args, **kwargs: marked.append((args, kwargs)),
    )

    assert scheduler_worker_failure.record_external_worker_outcome(
        job, execution_id="execution", expected_fire_owner="owner", worker_exited=True
    ) is True
    assert marked and marked[0][1]["success"] is False
