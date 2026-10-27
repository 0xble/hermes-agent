"""Execution-bound metadata at the real pre-worker dispatch boundaries."""

import concurrent.futures
import contextlib
import json
import logging
import sys
import threading
from types import SimpleNamespace

import pytest

from cron import executions, jobs, scheduler, scheduler_admission, scheduler_tick
from cron import scheduler_diagnostics as diagnostics
from cron.scheduler_provider import _profile_cron_scope


@pytest.fixture
def trace_home(tmp_path, monkeypatch, caplog):
    clock = SimpleNamespace(ns=1_000_000_000)
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic_ns=lambda: clock.ns), raising=False)
    caplog.set_level(logging.INFO, logger=diagnostics.__name__)
    with _profile_cron_scope(tmp_path):
        yield clock


def _events(caplog):
    return [record for record in caplog.records if record.name == diagnostics.__name__
            and record.getMessage().startswith("Cron dispatch ")]


def _stage(record):
    return record.cron_dispatch_stage


def test_saturated_pool_distinguishes_queue_wait_from_fire_claim(trace_home, monkeypatch, caplog):
    job = jobs.create_job(prompt="private prompt", schedule="* * * * *", deliver="local")
    entered = threading.Barrier(5)
    release = threading.Event()

    def occupied():
        entered.wait(timeout=5)
        assert release.wait(10)

    def body(item, **kwargs):
        executions.mark_execution_running(item["execution_id"])
        executions.finish_execution(item["execution_id"], success=True)
        return True

    monkeypatch.setattr(scheduler, "run_one_job", body)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        blockers = [pool.submit(occupied) for _ in range(4)]
        entered.wait(timeout=5)
        try:
            future = scheduler._submit_with_guard(
                job, pool, lambda item: scheduler_tick.process_due_job(item, None, None, False))
            assert future is not None
            row = executions.latest_execution(job["id"])
            assert row is not None
            assert row["status"] == "claimed"
            assert row["started_at"] is None
            assert row["handoff_pending"] == 0
            records = _events(caplog)
            assert [_stage(record) for record in records] == ["executor_queued"]
            assert records[0].cron_execution_id == row["id"]
            assert not future.done()
            trace_home.ns += 35 * 60 * 1_000_000_000
        finally:
            release.set()
        assert future.result(timeout=5) is True
        for blocker in blockers:
            blocker.result(timeout=5)

    records = _events(caplog)
    assert [_stage(record) for record in records] == [
        "executor_queued", "executor_enter", "fire_claim_enter", "fire_claim_exit"]
    assert {record.cron_execution_id for record in records} == {row["id"]}
    assert {record.cron_job_id for record in records} == {job["id"]}
    assert {record.cron_code_sha for record in records} == {row["code_sha"]}
    assert {record.cron_dispatch_lane for record in records} == {"general"}
    assert records[1].cron_monotonic_ns - records[0].cron_monotonic_ns == 35 * 60 * 1_000_000_000
    assert records[3].cron_monotonic_ns == records[2].cron_monotonic_ns
    assert not scheduler.is_job_running(job["id"])


def test_dispatch_metadata_never_serializes_job_or_secret_values(trace_home, caplog):
    private = "PRIVATE_PAYLOAD_MUST_NOT_APPEAR"
    job = {"id": "aabbccddeeff", "execution_id": "a" * 32, "name": private,
           "prompt": private, "env": {"API_KEY": private}, "fire_claim": {"by": private}}
    diagnostics.note_dispatch_stage(job, "executor_enter")
    record, = _events(caplog)
    assert record.cron_dispatch_stage == "executor_enter"
    assert record.cron_execution_id == job["execution_id"]
    assert record.cron_job_id == job["id"]
    assert record.cron_monotonic_ns == trace_home.ns
    assert private not in repr(record.__dict__)
    assert private not in record.getMessage()
    assert "executor_enter" in record.getMessage()
    assert job["execution_id"] in record.getMessage()
    assert str(trace_home.ns) in record.getMessage()
    assert {key for key in record.__dict__ if key.startswith("cron_")} == {
        "cron_dispatch_stage", "cron_execution_id", "cron_job_id", "cron_profile_home",
        "cron_dispatch_lane", "cron_code_sha", "cron_monotonic_ns"}


def test_profile_and_actual_lane_follow_each_queued_execution(trace_home, tmp_path, caplog):
    homes = [tmp_path / "a", tmp_path / "b"]
    expected = {}

    def body(item):
        executions.mark_execution_running(item["execution_id"])
        executions.finish_execution(item["execution_id"], success=True)
        return True

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            for lane in ("general", "reserved"):
                for home in (*homes, homes[0]):
                    with _profile_cron_scope(home):
                        job = jobs.create_job(prompt="private", schedule="* * * * *", deliver="local")
                        future = (scheduler_admission.submit_reserved(job, body) if lane == "reserved"
                                  else scheduler._submit_with_guard(job, pool, body))
                        assert future is not None and future.result(timeout=5) is True
                        row = executions.latest_execution(job["id"])
                        assert row is not None
                        expected[row["id"]] = (str(home.resolve()), lane, row["code_sha"])
    finally:
        scheduler_admission.discard_reserved_pools([str(home.resolve()) for home in homes])

    records = _events(caplog)
    assert len(records) == len(expected) * 2
    for execution_id, identity in expected.items():
        traced = [record for record in records if record.cron_execution_id == execution_id]
        assert [_stage(record) for record in traced] == ["executor_queued", "executor_enter"]
        assert {(record.cron_profile_home, record.cron_dispatch_lane, record.cron_code_sha)
                for record in traced} == {identity}


def test_failed_diagnostic_handler_does_not_change_claims_or_dispatch(trace_home, monkeypatch):
    attempted = []

    def broken_log(*args, **kwargs):
        attempted.append(kwargs["extra"]["cron_dispatch_stage"])
        raise RuntimeError("isolated diagnostic handler failure")

    def body(item, **kwargs):
        executions.mark_execution_running(item["execution_id"])
        executions.finish_execution(item["execution_id"], success=True)
        return True

    monkeypatch.setattr(diagnostics.logger, "info", broken_log)
    monkeypatch.setattr(scheduler, "run_one_job", body)
    job = jobs.create_job(prompt="private", schedule="* * * * *", deliver="local")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = scheduler._submit_with_guard(
            job, pool, lambda item: scheduler_tick.process_due_job(item, None, None, False))
        assert future is not None and future.result(timeout=5) is True
    row = executions.latest_execution(job["id"])
    assert row is not None and row["status"] == "completed"
    assert not scheduler.is_job_running(job["id"])
    assert attempted == ["executor_queued", "executor_enter", "fire_claim_enter", "fire_claim_exit"]


def test_worker_launch_bounds_secret_hydration_and_spawn(trace_home, tmp_path, monkeypatch, caplog):
    from tools import process_registry

    job = jobs.create_job(prompt="PRIVATE_JOB_PROMPT", schedule="* * * * *", deliver="local")
    row = executions.create_execution(job["id"], source="builtin")
    job["execution_id"] = row["id"]
    handoff_dir = tmp_path / "cron" / "external-workers"
    ack_path = handoff_dir / f"{row['id']}.ready"
    monkeypatch.setattr(scheduler, "load_config_readonly", lambda: {})
    monkeypatch.setattr(scheduler.immutable_releases, "worker_launch_spec",
                        lambda root, env: (sys.executable, tmp_path, env))
    monkeypatch.setattr(process_registry, "restart_safe_gateway_child_argv",
                        lambda argv, **kw: SimpleNamespace(argv=argv, mode="detached"))
    monkeypatch.setattr(process_registry, "systemd_user_bus_env", lambda env: env)
    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: False)
    monkeypatch.setattr("cron.scheduler_provider.routed_profile_fire", lambda: False)
    monkeypatch.setattr("tools.environments.local.build_subprocess_env",
                        lambda **kw: {"PRIVATE_ENV_KEY": "PRIVATE_ENV_VALUE"})
    monkeypatch.setattr("tools.environments.local.strip_launch_profile_env", lambda env: env)
    monkeypatch.setattr(scheduler, "_record_external_cron_worker", lambda *args, **kw: None)
    monkeypatch.setattr(scheduler, "_wait_for_external_cron_worker", lambda *args, **kw: True)

    @contextlib.contextmanager
    def secret_scope():
        assert _events(caplog), "secret hydration must have an execution-bound entry event"
        assert _stage(_events(caplog)[-1]) == "secret_scope_enter"
        trace_home.ns += 500 * 1_000_000_000
        yield

    def spawn(*args, **kwargs):
        assert _stage(_events(caplog)[-1]) == "spawn_enter"
        assert kwargs["env"]["PRIVATE_ENV_KEY"] == "PRIVATE_ENV_VALUE"
        trace_home.ns += 2 * 1_000_000_000
        ack_path.write_text(json.dumps({"execution_id": row["id"], "pid": 4321}))
        return SimpleNamespace(pid=4321, poll=lambda: None)

    monkeypatch.setattr(scheduler, "_fire_secret_scope", secret_scope)
    monkeypatch.setattr(scheduler.subprocess, "Popen", spawn)
    key = scheduler._inflight_key(job["id"])
    try:
        assert scheduler._launch_external_cron_worker(job) is True
    finally:
        with scheduler._running_lock:
            scheduler._external_worker_modes.pop(key, None)
            scheduler._restart_safe_external_worker_job_ids.discard(key)
            scheduler._restart_safe_waiter_job_ids.discard(key)

    records = _events(caplog)
    stages = [_stage(record) for record in records]
    assert stages == [
        "launch_enter", "launch_scope_ready", "handoff_fenced", "payload_written",
        "secret_scope_enter", "secret_scope_ready", "worker_env_ready", "launch_spec_ready",
        "spawn_enter", "spawn_exit", "ack_wait_enter", "acknowledged"]
    by_stage = {_stage(record): record for record in records}
    assert by_stage["secret_scope_ready"].cron_monotonic_ns - by_stage["secret_scope_enter"].cron_monotonic_ns == 500 * 1_000_000_000
    assert by_stage["spawn_exit"].cron_monotonic_ns - by_stage["spawn_enter"].cron_monotonic_ns == 2 * 1_000_000_000
    assert {record.cron_execution_id for record in records} == {row["id"]}
    rendered = "\n".join(record.getMessage() for record in records)
    assert "PRIVATE_JOB_PROMPT" not in rendered
    assert "PRIVATE_ENV_VALUE" not in rendered
    assert "PRIVATE_ENV_KEY" not in rendered
    assert not ack_path.exists()
