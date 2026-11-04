"""Create-time checksum rejection and pre-send freshness holds."""
from datetime import UTC, datetime, timedelta

import pytest

from cron import jobs
from cron.scheduled_send import validate_scheduled_send_inputs

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
GOOD = "a" * 64


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(jobs, "_hermes_now", lambda: NOW)
    return tmp_path


def test_model_tool_handler_forwards_scheduled_send_contract(store, tmp_path):
    from tools import cronjob_tools
    from unittest.mock import patch
    spec = {"channel": "slack", "conversation": "C1", "account": "lpg", "recipient": "U1",
            "approved_at": NOW.isoformat(), "script": str(tmp_path / "check.py")}
    with patch.object(cronjob_tools, "cronjob", return_value={}) as called:
        import json
        cronjob_tools._cronjob_handler({"action": "create", "payload_path": "/body.txt",
            "payload_sha256": GOOD, "send_freshness_json": json.dumps(spec)})
    assert called.call_args.kwargs["payload_sha256"] == GOOD
    assert called.call_args.kwargs["payload_path"] == "/body.txt"
    assert json.loads(called.call_args.kwargs["send_freshness_json"]) == spec


def test_create_rejects_short_declared_checksum(store):
    prompt = "Expected SHA-256: " + ("ab" * 30) + "c"
    with pytest.raises(ValueError, match="64 hex"):
        jobs.create_job(prompt=prompt, schedule="in 10m", name="bad-digest")
    assert jobs.list_jobs() == []


def test_create_accepts_a_64_hex_checksum(store):
    job = jobs.create_job(
        prompt=f"Expected SHA-256: {GOOD}", schedule="in 10m", name="good-digest")
    assert job["id"]
    assert jobs.get_job(job["id"])["prompt"].endswith(GOOD)


def test_update_rejects_a_bad_checksum_without_touching_the_stored_prompt(store):
    job = jobs.create_job(prompt="plain report", schedule="in 10m")
    with pytest.raises(ValueError, match="64 hex"):
        jobs.update_job(job["id"], {"prompt": "Expected SHA-256: abcdef"})
    assert jobs.get_job(job["id"])["prompt"] == "plain report"


def test_pause_of_a_legacy_bad_checksum_is_not_revalidated(store):
    run_at = (NOW + timedelta(minutes=10)).isoformat()
    job = {
        "id": "legacy", "name": "legacy", "prompt": "Expected SHA-256: " + "ab" * 30 + "cd",
        "schedule": {"kind": "once", "run_at": run_at}, "next_run_at": run_at,
        "enabled": True, "state": "scheduled", "repeat": {"times": 1, "completed": 0},
        "deliver": "local",
    }
    jobs.save_jobs([job])
    paused = jobs.pause_job(job["id"], reason="operator stop")
    assert paused["enabled"] is False
    assert paused["prompt"].startswith("Expected SHA-256:")


def test_payload_file_must_match_its_checksum(store, tmp_path):
    payload = tmp_path / "approved.txt"
    payload.write_bytes(b"hello")
    with pytest.raises(ValueError, match="does not match"):
        jobs.create_job(
            prompt="send", schedule="in 10m",
            payload_sha256=GOOD, payload_path=str(payload))


def test_send_freshness_requires_an_existing_script(store, tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        validate_scheduled_send_inputs(send_freshness={
            "channel": "slack", "conversation": "C1", "account": "lpg", "recipient": "U1",
            "approved_at": NOW.isoformat(), "script": str(tmp_path / "missing.py")})


def test_stale_freshness_holds_the_payload_and_does_not_autofire(store, monkeypatch):
    script = store / "check.py"
    script.write_text("print('ok')\n")
    run_at = (NOW - timedelta(seconds=30)).isoformat()
    job = {
        "id": "late-send", "name": "james update", "prompt": "send the approved text",
        "schedule": {"kind": "once", "run_at": run_at}, "next_run_at": run_at,
        "enabled": True, "state": "scheduled", "repeat": {"times": 1, "completed": 0},
        "deliver": "origin", "origin": {"platform": "telegram", "chat_id": "1", "thread_id": "2"},
        "send_freshness": {
            "channel": "slack", "conversation": "C1", "account": "lpg", "recipient": "U1",
            "approved_at": NOW.isoformat(), "script": str(script)},
        "run_claim": {"at": run_at, "by": "ticker"},
    }
    jobs.save_jobs([job])
    monkeypatch.setattr(
        "cron.scheduled_send.assess_freshness",
        lambda *_a, **_k: {"stale": True, "checked": True, "reason": "huddle active"},
    )
    assert jobs.hold_stale_person_send(job) is True
    held = jobs.get_job("late-send")
    assert held["state"] == "missed"
    assert held["enabled"] is False
    assert held["prompt"] == "send the approved text"
    assert held["run_claim"] is None
    assert jobs.get_due_jobs() == []


def test_send_now_still_rereads_the_target(store, monkeypatch):
    script = store / "check.py"
    script.write_text("print('ok')\n")
    run_at = NOW.isoformat()
    job = {
        "id": "send-now", "name": "james update", "prompt": "send",
        "schedule": {"kind": "once", "run_at": run_at}, "next_run_at": run_at,
        "manual_run_at": run_at, "enabled": True, "state": "scheduled",
        "send_freshness": {"channel": "slack", "conversation": "C1", "account": "lpg", "recipient": "U1",
            "approved_at": NOW.isoformat(), "script": str(script)},
    }
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        return {"stale": True, "reason": "should not run"}

    monkeypatch.setattr("cron.scheduled_send.assess_freshness", boom)
    jobs.save_jobs([job])
    assert jobs.hold_stale_person_send(job) is True
    assert called["n"] == 1


def test_trigger_after_miss_is_due_again(store):
    stale_at = (NOW - timedelta(hours=3)).isoformat()
    jobs.save_jobs([{
        "id": "kept", "name": "kept", "prompt": "the approved body",
        "schedule": {"kind": "once", "run_at": stale_at}, "next_run_at": stale_at,
        "enabled": True, "state": "scheduled", "repeat": {"times": 1, "completed": 0},
        "deliver": "local",
    }])
    assert jobs.get_due_jobs() == []
    held = jobs.get_job("kept")
    assert held["state"] == "missed"
    from types import SimpleNamespace
    from hermes_cli.cron import cron_resume
    assert cron_resume(SimpleNamespace(job_id="kept", run_now=True, run_at=None)) == 0
    due = jobs.get_due_jobs()
    assert [row["id"] for row in due] == ["kept"]
    assert jobs.get_job("kept")["prompt"] == "the approved body"

@pytest.mark.parametrize("digest", ["z" * 64, "a" * 63 + "z", "a" * 65, "abc-def", ""])
def test_create_rejects_malformed_declared_checksum(store, digest):
    with pytest.raises(ValueError, match="64 hex"):
        jobs.create_job(prompt="Expected SHA-256: " + digest, schedule="in 10m")


@pytest.mark.parametrize("stdout", ["{}", '{"stale": false}', '{"stale": false, "checked": false}'])
def test_freshness_subprocess_cannot_fail_open(stdout, monkeypatch):
    from types import SimpleNamespace
    from cron.scheduled_send import run_freshness_script
    monkeypatch.setattr("cron.scheduled_send.subprocess.run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=stdout, stderr=""))
    assert run_freshness_script({"script": "check.py"}, {})["stale"] is True


@pytest.mark.parametrize("sender", [None, "fixture-sender"])
def test_freshness_reader_preserves_optional_sender_contract(sender, monkeypatch):
    import json
    from types import SimpleNamespace
    from cron.scheduled_send import run_freshness_script

    spec = {"script": "check.py", "channel": "imessage", "conversation": "42",
            "recipient": "fixture-recipient", "approved_at": NOW.isoformat()}
    if sender is not None:
        spec["sender"] = sender
    captured = {}

    def reader(*args, **kwargs):
        captured.update(json.loads(kwargs["input"]))
        return SimpleNamespace(returncode=0, stdout='{"checked": true, "stale": false}', stderr="")

    monkeypatch.setattr("cron.scheduled_send.subprocess.run", reader)
    result = run_freshness_script(spec, {"id": "fixture-job", "next_run_at": NOW.isoformat()})
    assert result["checked"] is True and result["stale"] is False
    assert captured == {
        **{key: value for key, value in spec.items() if key != "script"},
        "job_id": "fixture-job", "scheduled_at": NOW.isoformat(),
    }


@pytest.mark.parametrize("stale", [True, False])
def test_stale_snapshot_cannot_hold_reapproved_job(store, monkeypatch, stale):
    job = {
        "id": "edited", "prompt": "original", "enabled": True,
        "state": "scheduled", "next_run_at": NOW.isoformat(),
        "schedule": {"kind": "once", "run_at": NOW.isoformat()},
        "send_freshness": {"channel": "slack"},
    }
    jobs.save_jobs([dict(job, prompt="reapproved")])
    monkeypatch.setattr("cron.scheduled_send.assess_freshness", lambda *a: {"stale": stale})
    assert jobs.hold_stale_person_send(job) is True
    assert jobs.get_job("edited")["enabled"] is True
    assert jobs.get_job("edited")["prompt"] == "reapproved"


def test_outage_is_provenance_not_permission_to_send_hours_late(store, monkeypatch):
    run_at = (NOW - timedelta(hours=3)).isoformat()
    jobs.save_jobs([{
        "id": "outage", "prompt": "keep me", "enabled": True, "state": "scheduled",
        "schedule": {"kind": "once", "run_at": run_at}, "next_run_at": run_at,
        "repeat": {"times": 1, "completed": 0}, "deliver": "local",
    }])
    monkeypatch.setattr(jobs.store_health, "outage_covers", lambda *a: True)
    assert jobs.get_due_jobs() == []
    assert jobs.get_job("outage")["state"] == "missed"
    assert "store outage" in jobs.get_job("outage")["missed_reason"]

def test_deadline_is_rechecked_after_a_fresh_live_read(store, monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock["now"])
    due = (NOW - timedelta(seconds=jobs.ONESHOT_GRACE_SECONDS - 1)).isoformat()
    job = {"id": "late-read", "prompt": "approved", "enabled": True,
           "next_run_at": due, "schedule": {"kind": "once", "run_at": due},
           "send_freshness": {"channel": "slack"}, "deliver": "local"}
    jobs.save_jobs([job])

    def check(*a):
        clock["now"] += timedelta(seconds=2)
        return {"stale": False, "checked": True}

    monkeypatch.setattr("cron.scheduled_send.assess_freshness", check)
    assert jobs.hold_stale_person_send(job) is True
    assert jobs.get_job(job["id"])["state"] == "missed"


def test_rearm_cannot_reactivate_a_legacy_bad_checksum(store):
    past = (NOW - timedelta(hours=3)).isoformat()
    jobs.save_jobs([{"id": "legacy-bad", "prompt": "Expected SHA-256: " + "a" * 61,
                     "enabled": False, "state": "missed", "next_run_at": past,
                     "schedule": {"kind": "once", "run_at": past}}])
    with pytest.raises(ValueError, match="64 hex"):
        jobs.rearm_oneshot("legacy-bad", NOW.isoformat())
    assert jobs.get_job("legacy-bad")["enabled"] is False


@pytest.mark.parametrize("changes", [{"channel": "unsupported"}, {"account": ""},
    {"conversation": "not-a-slack-id"}, {"recipient": {}}, {"recipient": "name"}])
def test_freshness_invalid_target_is_rejected_before_saving(store, changes):
    script = store / "check.py"
    script.write_text("print('ok')")
    spec = {"channel": "slack", "account": "lpg", "conversation": "C1", "recipient": "U1",
            "script": str(script), "approved_at": NOW.isoformat(), **changes}
    with pytest.raises(ValueError):
        jobs.create_job(prompt="send", schedule="in 5m", send_freshness=spec)
    assert jobs.load_jobs() == []


@pytest.mark.parametrize("channel,conversation,recipient,extra", [
    ("imessage", "42", "+15551234567", {}),
    ("telegram", "42", "99", {"account": "personal"}),
    ("email", "abcdef", "family@example.com", {"account": "personal", "reviewed_message_ids": ["123abc"]}),
    ("quo", "PNexample", "+15551234567", {"account": "business"}),
])
def test_non_slack_sends_need_no_invented_shared_slack_binding(store, tmp_path,
                                                               channel, conversation, recipient, extra):
    reader = tmp_path / "reader.py"
    reader.write_text("pass\n")
    spec = {"channel": channel, "conversation": conversation, "recipient": recipient,
            "approved_at": NOW.isoformat(), "script": str(reader), **extra}
    made = jobs.create_job(prompt="approved family reply", schedule="in 10m", send_freshness=spec)
    assert jobs.get_job(made["id"])["send_freshness"] == spec


def test_declared_but_incomplete_shared_activity_context_is_rejected(store, tmp_path):
    reader = tmp_path / "reader.py"
    reader.write_text("pass\n")
    with pytest.raises(ValueError, match="activity_context"):
        jobs.create_job(prompt="approved reply", schedule="in 10m", send_freshness={
            "channel": "imessage", "conversation": "42", "recipient": "+15551234567",
            "approved_at": NOW.isoformat(), "script": str(reader), "activity_context": {}})
