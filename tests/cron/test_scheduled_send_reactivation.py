"""Rearmed occurrences retain payloads; declarations do not ban ordinary jobs."""
import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from cron import delivery_queue, jobs, scheduler

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron/jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron/output")
    monkeypatch.setattr(delivery_queue, "DELIVERY_DB", tmp_path / "cron/deliveries.db")
    monkeypatch.setattr("cron.incidents.EXECUTIONS_FILE", tmp_path / "cron/executions.db")
    monkeypatch.setattr("cron.executions.EXECUTIONS_FILE", tmp_path / "cron/executions.db")
    clock = {"now": NOW}
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock["now"])
    return tmp_path, clock


def decision_notice(job):
    notice = delivery_queue.get_status(f"oneshot-missed:{job['id']}:{job['next_run_at']}")
    assert notice is not None and notice["status"] == "pending"
    assert notice["for_failure"] and not notice["terminal_gate"]
    assert json.loads(notice["job_json"])["origin"] == job["origin"]
    assert "Payload kept" in notice["content"]
    assert f"resume {job['id']} --run-now" in notice["content"]
    assert f"remove {job['id']}" in notice["content"]
    return notice


def test_never_started_rearm_miss_keeps_payload_history_and_one_decision(store):
    root, clock = store
    payload = root / "approved.txt"
    payload.write_bytes(b"Exact approved body.\n")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    job = jobs.create_job(prompt="Send the exact approved body.", schedule="in 10m",
                          payload_path=str(payload), payload_sha256=digest,
                          origin={"platform": "telegram", "chat_id": "owner", "thread_id": "topic"})
    previous = (NOW - timedelta(days=1)).isoformat()
    jobs.update_job(job["id"], {"last_run_at": previous, "enabled": False,
                                "state": "completed", "repeat": {"times": 1, "completed": 1}})
    armed = jobs.rearm_oneshot(job["id"], NOW + timedelta(minutes=1))
    assert armed["repeat"]["completed"] == 0
    clock["now"] = NOW + timedelta(minutes=5)
    assert jobs.get_due_jobs() == []
    held = jobs.get_job(job["id"])
    assert held is not None
    assert held["state"] == "missed" and not held["enabled"]
    assert held["last_run_at"] == previous
    for field in ("prompt", "payload_path", "payload_sha256"):
        assert held[field] == job[field]
    assert payload.read_bytes() == b"Exact approved body.\n"
    notice = decision_notice(held)
    assert jobs.get_due_jobs() == []
    assert decision_notice(held) == notice


@pytest.mark.parametrize("prompt", [
    "Weekly: verify release (sha256: see manifest)",
    "Run sha256=$(cat x) after building",
    "Expected sha256: `<hash>` from the release manifest",
    "Template sha256: ${digest}",
    "Expected SHA-256: {{ checksum }}",
])
def test_recurring_prose_templates_and_commands_can_create_update_resume_and_trigger(store, prompt):
    job = jobs.create_job(prompt=prompt, schedule="every 1h")
    assert jobs.update_job(job["id"], {"prompt": prompt})["prompt"] == prompt
    assert jobs.pause_job(job["id"])["enabled"] is False
    assert jobs.resume_job(job["id"])["enabled"] is True
    assert jobs.trigger_job(job["id"])["prompt"] == prompt


@pytest.mark.parametrize("field,value", [("payload_sha256", "a" * 61),
                                         ("prompt", "Expected SHA-256: " + "a" * 61)])
@pytest.mark.parametrize("action", ["create", "update", "resume", "trigger", "rearm"])
def test_explicit_malformed_digests_cannot_persist_or_reactivate(store, field, value, action):
    if action == "create":
        with pytest.raises(ValueError, match="64 hex"):
            jobs.create_job(schedule="in 10m", **{"prompt": "approved", field: value})
        assert jobs.list_jobs() == []
        return
    job = jobs.create_job(prompt="approved", schedule="in 10m")
    if action == "update":
        with pytest.raises(ValueError, match="64 hex"):
            jobs.update_job(job["id"], {field: value})
        assert jobs.get_job(job["id"]).get(field) == job.get(field)
        return
    # Legacy malformed records may be paused, but cannot be reactivated.
    jobs.save_jobs([{**job, field: value}])
    assert jobs.pause_job(job["id"])["enabled"] is False
    operations = {"resume": jobs.resume_job, "trigger": jobs.trigger_job,
                  "rearm": lambda jid: jobs.rearm_oneshot(jid, NOW + timedelta(minutes=1))}
    with pytest.raises(ValueError, match="64 hex"):
        operations[action](job["id"])
    assert jobs.get_job(job["id"])["enabled"] is False


def test_checker_exception_holds_before_dispatch_and_records_terminal_evidence(store, monkeypatch):
    root, _clock = store
    script = root / "scripts/reader.py"
    script.parent.mkdir()
    script.write_text("pass\n")
    job = jobs.create_job(prompt="approved body", schedule="in 1m",
                          origin={"platform": "telegram", "chat_id": "owner", "thread_id": "topic"},
                          send_freshness={"channel": "imessage", "conversation": "42",
                                          "recipient": "fixture", "approved_at": NOW.isoformat(),
                                          "script": str(script)})
    job = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
    assert isinstance(job, dict) and job["fire_claim"]
    checker = Mock(side_effect=RuntimeError("reader unavailable"))
    launch = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr("cron.scheduled_send.run_freshness_script", checker)
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", launch)
    assert scheduler.run_one_job(job) is True
    checker.assert_called_once()
    launch.assert_not_called()
    held = jobs.get_job(job["id"])
    assert held["state"] == "missed" and not held["enabled"]
    assert held.get("run_claim") is None and held.get("fire_claim") is None
    assert held["prompt"] == job["prompt"]
    assert "RuntimeError: reader unavailable" in held["missed_reason"]
    notice = decision_notice(held)
    assert "reader unavailable" in notice["content"]
    execution = scheduler.get_execution(job["execution_id"])
    assert execution["status"] == "failed"
    assert "held; payload kept" in execution["error"]
