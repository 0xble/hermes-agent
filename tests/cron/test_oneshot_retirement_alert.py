"""Retiring a never-finished one-shot must durably notify its owner, not fire late."""
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from cron import delivery_queue, jobs, scheduler
from gateway.config import GatewayConfig, Platform, PlatformConfig

NOW = datetime(2026, 11, 2, 2, 19, 57, tzinfo=UTC)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron/jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron/output")
    monkeypatch.setattr(jobs, "_hermes_now", lambda: NOW)
    monkeypatch.setattr(delivery_queue, "DELIVERY_DB", tmp_path / "cron/deliveries.db")
    monkeypatch.setattr("cron.incidents.EXECUTIONS_FILE", tmp_path / "cron/executions.db")
    config = GatewayConfig()
    config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=True)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    return tmp_path


def oneshot():
    run_at = (NOW - timedelta(seconds=297)).isoformat()
    return {"id": "missed-one", "name": "time-sensitive one-shot", "prompt": "do it",
            "schedule": {"kind": "once", "run_at": run_at}, "next_run_at": run_at,
            "last_run_at": None, "enabled": True, "state": "scheduled",
            "repeat": {"times": 1, "completed": 0}, "deliver": "local",
            "origin": {"platform": "telegram", "chat_id": "owner-chat", "thread_id": "owner-topic"}}


@pytest.mark.parametrize("path", ["missed", "wedged-scan", "wedged-expired", "wedged-claim"])
@pytest.mark.parametrize("origin", [True, False])
def test_retirement_queues_one_owner_failure_even_across_removal_crash(store, monkeypatch, path, origin):
    job = oneshot()
    if not origin:
        job.pop("origin")
        monkeypatch.setattr("cron.scheduler_delivery._iter_home_target_platforms", lambda: iter(["telegram"]))
        monkeypatch.setattr("cron.scheduler_delivery._get_home_target_chat_id", lambda _: "fallback-chat")
        monkeypatch.setattr("cron.scheduler_delivery._get_home_target_thread_id", lambda _: "fallback-topic")
    if path.startswith("wedged"):
        job["repeat"]["completed"] = 1
        # Claim recovery also handles a consumed dispatch without a remaining next_run_at.
        if path == "wedged-claim":
            job["next_run_at"] = None
        elif path == "wedged-scan":
            job["next_run_at"] = (NOW - timedelta(seconds=60)).isoformat()
    jobs.save_jobs([job])
    action = (lambda: jobs.claim_dispatch(job["id"])) if path == "wedged-claim" else jobs.get_due_jobs
    real_save = jobs.save_jobs
    crashed = False

    def crash_before_removal(rows, **kwargs):
        nonlocal crashed
        if kwargs.get("removed_ids") and not crashed:
            crashed = True
            raise OSError("simulated restart before removal commit")
        return real_save(rows, **kwargs)

    # The queue must commit before removal: a restart in between reuses its receipt.
    with monkeypatch.context() as m:
        m.setattr(jobs, "save_jobs", crash_before_removal)
        if path == "wedged-claim":
            with pytest.raises(OSError, match="simulated restart"):
                action()
        else:
            assert action() == []
    assert jobs.get_job(job["id"]) is not None
    # A completed send receipt also survives replay before the removal commits.
    with patch("cron.scheduler_delivery._deliver_standalone") as send:
        assert scheduler.drain_delivery_queue({}, None) == 1
        assert send.call_count == 1
        target, content = send.call_args.args[:2]
        assert target.chat_id == ("owner-chat" if origin else "fallback-chat")
        assert target.thread_id == ("owner-topic" if origin else "fallback-topic")
        for value in (job["id"], job["name"], job["schedule"]["run_at"], "297", "recreate to run"):
            assert value in content
        if path == "missed":
            assert "outside" in content and "not run" in content
        else:
            assert "interrupted" in content and "partial effects" in content
        action()
        assert jobs.get_job(job["id"]) is None
        assert list((store / "cron/output" / job["id"]).glob("*.md"))
        assert scheduler.drain_delivery_queue({}, None) == 0
        assert send.call_count == 1
    from cron.incidents import list_incidents
    assert len(list_incidents()) == 1


@pytest.mark.parametrize("path", ["missed", "wedged-scan", "wedged-expired", "wedged-claim"])
def test_outbox_failure_preserves_job_until_retry(store, monkeypatch, path):
    job = oneshot()
    if path.startswith("wedged"):
        job["repeat"]["completed"] = 1
        if path == "wedged-scan":
            job["next_run_at"] = (NOW - timedelta(seconds=60)).isoformat()
    jobs.save_jobs([job])
    action = (lambda: jobs.claim_dispatch(job["id"])) if path == "wedged-claim" else jobs.get_due_jobs
    with monkeypatch.context() as m:
        def fail(*args, **kwargs):
            raise OSError("outbox unavailable")
        m.setattr(delivery_queue, "enqueue", fail)
        if path == "wedged-claim":
            with pytest.raises(OSError, match="outbox unavailable"):
                action()
        else:
            assert action() == []
    assert jobs.get_job(job["id"]) is not None
    action()
    assert jobs.get_job(job["id"]) is None
    with patch("cron.scheduler_delivery._deliver_standalone") as send:
        assert scheduler.drain_delivery_queue({}, None) == 1
        assert send.call_count == 1


def test_no_configured_chat_retains_the_normal_incident_fallback(store, monkeypatch):
    from cron.incidents import list_incidents
    job = oneshot()
    job.pop("origin")
    monkeypatch.setattr("cron.scheduler_delivery._iter_home_target_platforms", lambda: iter([]))
    jobs.save_jobs([job])
    assert jobs.get_due_jobs() == []
    assert jobs.get_job(job["id"]) is None
    rows = list_incidents()
    assert len(rows) == 1
    assert rows[0]["job_id"] == job["id"]
    assert "missed-oneshot" in rows[0]["error"]
    assert "not run; recreate to run" in rows[0]["error"]


def test_startup_context_survives_heartbeat_replacement(store):
    from cron.oneshot_retirement import record_ticker_start
    previous = NOW - timedelta(minutes=30)
    jobs._write_marker("ticker_heartbeat", f"{previous.timestamp()} 123", ".hb_")
    record_ticker_start()
    jobs.record_ticker_heartbeat()
    job = oneshot()
    job.update(execution_id="dead-run", failure_deliver="local", output_local_only=True)
    jobs.save_jobs([job])
    assert jobs.get_due_jobs() == []
    with patch("cron.scheduler_delivery._deliver_standalone") as send:
        assert scheduler.drain_delivery_queue({}, None) == 1
        content = send.call_args.args[1]
        assert f"Scheduler started at {NOW.isoformat()}" in content
        assert previous.isoformat() in content
        assert "downtime onset is not known" in content


@pytest.mark.parametrize("kind", ["once", "interval"])
def test_runnable_jobs_do_not_emit_retirement_notices(store, kind):
    job = oneshot()
    if kind == "once":
        job["next_run_at"] = (NOW - timedelta(seconds=60)).isoformat()
        job["schedule"]["run_at"] = job["next_run_at"]
    else:
        job["schedule"] = {"kind": "interval", "minutes": 60}
        job["repeat"]["times"] = None
        job["next_run_at"] = (NOW - timedelta(hours=2)).isoformat()
    jobs.save_jobs([job])
    assert [row["id"] for row in jobs.get_due_jobs()] == [job["id"]]
    assert jobs.get_job(job["id"]) is not None
    assert scheduler.drain_delivery_queue({}, None) == 0
