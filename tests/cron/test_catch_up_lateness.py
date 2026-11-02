"""Window-bound recurring occurrences expire, including while waiting for a worker."""
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from cron import delivery_queue, executions, jobs, scheduler
from cron.scheduler_tick import process_due_job
from gateway.config import GatewayConfig, Platform, PlatformConfig

NOW = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
FIELD = "max_catch_up_lateness_seconds"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron/jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron/output")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron/executions.db")
    monkeypatch.setattr(delivery_queue, "DELIVERY_DB", tmp_path / "cron/deliveries.db")
    monkeypatch.setattr("cron.incidents.EXECUTIONS_FILE", tmp_path / "cron/executions.db")
    monkeypatch.setattr(jobs, "_hermes_now", lambda: NOW)
    config = GatewayConfig()
    config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=True)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    return tmp_path


def recurring(age, limit=300):
    slot = (NOW - timedelta(seconds=age)).isoformat()
    job = {"id": "window-job", "name": "maintenance window", "prompt": "promote",
           "schedule": {"kind": "interval", "minutes": 60}, "next_run_at": slot,
           "last_run_at": None, "enabled": True, "state": "scheduled",
           "repeat": {"times": None, "completed": 0}, "deliver": "local",
           "failure_deliver": "local", "output_local_only": True,
           "origin": {"platform": "telegram", "chat_id": "owner", "thread_id": "topic"}}
    if limit is not None:
        job[FIELD] = limit
    return job


@pytest.mark.parametrize("path", ["scan", "queued", "provider", "detached"])
def test_expired_occurrence_skips_and_alerts_owner_once(store, monkeypatch, path, make_cron_provider):
    job = recurring(16 * 3600 if path in {"scan", "provider"} else 299)
    slot = job["next_run_at"]
    jobs.save_jobs([job])
    launch = Mock(return_value=True)
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", launch)
    if path == "scan":
        assert jobs.get_due_jobs() == []
    elif path == "provider":
        assert make_cron_provider().claim_fire(job["id"]) is None
    else:
        due = jobs.get_due_jobs()[0]
        jobs.advance_next_runs([job["id"]])
        due["execution_id"] = executions.create_execution(
            job["id"], source="test", scheduled_instant=slot)["id"]
        if path == "detached":
            claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
            claimed.update(execution_id=due["execution_id"], _scheduled_instant=due["_scheduled_instant"])
            monkeypatch.setenv("_HERMES_CRON_EXTERNAL_WORKER", due["execution_id"])
        monkeypatch.setattr(jobs, "_hermes_now", lambda: NOW + timedelta(seconds=2))
        if path == "detached":
            assert scheduler.run_one_job(claimed)
        else:
            assert process_due_job(due, None, None, False)
        assert executions.get_execution(due["execution_id"])["error"].startswith("skipped: missed window")
    launch.assert_not_called()
    saved = jobs.get_job(job["id"])
    assert saved["last_status"] == "skipped"
    result = saved["last_missed_window"]
    assert result["scheduled_at"] == slot
    assert result["lateness_seconds"] > result["max_catch_up_lateness_seconds"] == 300
    assert datetime.fromisoformat(saved["next_run_at"]) > NOW
    assert not saved.get("pending_slot") and not saved.get("fire_claim")
    assert saved["repeat"]["completed"] == 0 and saved["last_run_at"] is None
    assert list((store / "cron/output" / job["id"]).glob("*.md"))
    if path == "provider":
        # A late hosted retry must not execute the newly advanced future slot early.
        assert make_cron_provider().claim_fire(job["id"]) is None
    with patch("cron.scheduler_delivery._deliver_standalone") as send:
        assert scheduler.drain_delivery_queue({}, None) == 1
        assert send.call_count == 1
        target, content = send.call_args.args[:2]
        assert target.chat_id == "owner" and target.thread_id == "topic"
        assert "missed window" in content and slot in content and "300" in content
        assert jobs.get_due_jobs() == []
        assert scheduler.drain_delivery_queue({}, None) == 0
        assert send.call_count == 1


@pytest.mark.parametrize("age, limit, manual", [(299, 300, False), (300, 300, False),
                                                 (0, 0, False), (16 * 3600, None, False),
                                                 (16 * 3600, 0, True)])
def test_eligible_and_unbounded_jobs_still_dispatch(store, monkeypatch, age, limit, manual):
    job = recurring(age, limit)
    if manual:
        job["manual_run_at"] = job["next_run_at"]
    jobs.save_jobs([job])
    due = jobs.get_due_jobs()
    assert len(due) == 1
    jobs.advance_next_runs([job["id"]])
    due[0]["execution_id"] = executions.create_execution(job["id"], source="test")["id"]
    launch = Mock(return_value=True)
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", launch)
    assert process_due_job(due[0], None, None, False)
    launch.assert_called_once()
    assert scheduler.drain_delivery_queue({}, None) == 0


def test_within_limit_reaches_worker_execution(store, monkeypatch):
    job = recurring(299)
    job["last_missed_window"] = {"scheduled_at": (NOW - timedelta(days=1)).isoformat()}
    jobs.save_jobs([job])
    due = jobs.get_due_jobs()[0]
    jobs.advance_next_runs([due["id"]])
    # The tick's real worker is already registered as running while its pending slot is queued.
    monkeypatch.setattr(jobs, "_job_running_in_this_process", lambda _job_id: True)
    due["execution_id"] = executions.create_execution(due["id"], source="test")["id"]
    with patch.object(scheduler, "run_one_job", return_value=True) as run:
        assert process_due_job(due, adapters={}, loop=None, verbose=False)
    run.assert_called_once()
    assert run.call_args.args[0]["_scheduled_instant"] == job["next_run_at"]
    assert scheduler.drain_delivery_queue({}, None) == 0


def test_cron_window_advances_to_next_wall_clock_slot(store):
    slot = "2026-11-01T03:58:00+00:00"
    job = recurring(0, 1800)
    job.update(schedule={"kind": "cron", "expr": "58 3 * * *"},
               timezone="UTC", next_run_at=slot)
    jobs.save_jobs([job])
    assert jobs.get_due_jobs() == []
    saved = jobs.get_job(job["id"])
    assert saved["last_missed_window"]["scheduled_at"] == slot
    assert saved["next_run_at"] == "2026-11-02T03:58:00+00:00"


def test_catch_up_limit_does_not_change_oneshot_grace(store):
    job = recurring(119, 0)
    job["schedule"] = {"kind": "once", "run_at": job["next_run_at"]}
    job["repeat"]["times"] = 1
    jobs.save_jobs([job])
    assert len(jobs.get_due_jobs()) == 1
    assert scheduler.drain_delivery_queue({}, None) == 0


@pytest.mark.parametrize("value", [-1, True, "hello", float("inf"), float("nan")])
def test_invalid_policy_never_persists(store, value):
    with pytest.raises(ValueError, match=FIELD):
        jobs.create_job("run", "every 1h", **{FIELD: value})
    jobs.save_jobs([recurring(10)])
    with pytest.raises(ValueError, match=FIELD):
        jobs.update_job("window-job", {FIELD: value})
    assert jobs.get_job("window-job")[FIELD] == 300


@pytest.mark.parametrize("fault", ["outbox", "store"])
def test_expiration_is_retryable_and_alert_receipt_survives_restart(store, monkeypatch, fault):
    job = recurring(3600)
    jobs.save_jobs([job])
    with monkeypatch.context() as m:
        def fail(*args, **kwargs):
            raise OSError("simulated unavailable " + fault)
        m.setattr(delivery_queue if fault == "outbox" else jobs,
                  "enqueue" if fault == "outbox" else "save_jobs", fail)
        assert jobs.get_due_jobs() == []
    assert jobs.get_job(job["id"])["next_run_at"] == job["next_run_at"]
    with patch("cron.scheduler_delivery._deliver_standalone") as send:
        before = scheduler.drain_delivery_queue({}, None)
        assert before == (1 if fault == "store" else 0)
        assert jobs.get_due_jobs() == []
        assert scheduler.drain_delivery_queue({}, None) == 1 - before
        assert send.call_count == 1
    assert jobs.get_job(job["id"])["last_status"] == "skipped"


def test_policy_round_trip_and_clear(store):
    job = jobs.create_job("run", "every 1h", **{FIELD: 0})
    assert jobs.get_job(job["id"])[FIELD] == 0
    assert jobs.update_job(job["id"], {FIELD: 1.5})[FIELD] == 1.5
    assert jobs.update_job(job["id"], {FIELD: None}).get(FIELD) is None
    assert jobs.update_job(job["id"], {FIELD: ""}).get(FIELD) is None
