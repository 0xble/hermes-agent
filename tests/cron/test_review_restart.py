"""Only final sends are uncertain across a gateway restart, never held reviews."""
import json
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("expired", [False, True])
def test_restart_during_review_replays_report_and_writes_delivery_receipt(tmp_path, monkeypatch, expired):
    from cron import delivery_queue as queue, scheduler_review as review

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "cron/deliveries.db")
    monkeypatch.setattr(review, "_shutdown", threading.Event())
    clock = [time.time()]
    monkeypatch.setattr(review, "time", SimpleNamespace(time=lambda: clock[0],
                       monotonic=time.monotonic, sleep=time.sleep))
    job = {"id": "report", "deliver": "local", "_review_run_id": "run",
           "_review_deadline_epoch": clock[0] + 60,
           "review": {"subject": "topic", "contract": "report"}}
    subject = {"session_key": "key", "session_id": "sid", "chat_id": "42",
               "thread_id": "123", "user_id": "42"}
    monkeypatch.setattr(review, "_resolve_review_subject", lambda _: subject)
    monkeypatch.setattr("cron.review.load_contract", lambda _: {})
    entered, release = threading.Event(), threading.Event()
    def hold(*args):
        entered.set()
        assert release.wait(5)
        raise review.ReviewAbandoned("gateway exited during review")
    monkeypatch.setattr(review, "_run_review_hop", hold)
    workers = []
    thread = threading.Thread
    def spawn(*args, **kwargs):
        worker = thread(*args, **kwargs)
        workers.append(worker)
        return worker
    monkeypatch.setattr(queue.threading, "Thread", spawn)
    sent = []
    def final_send(job, text, **kwargs):
        assert queue.get_status("run")["status"] == "delivering"
        sent.append(text)
        job["_last_delivery_message_ids"] = ["message"]
    monkeypatch.setattr("cron.scheduler_delivery._deliver_result", final_send)
    send = lambda job, text, failure: review._deliver_reviewed_result(job, text, {}, None)
    queue.enqueue("run", job, "original")
    try:
        assert queue.drain(send) == 1
        assert entered.wait(5)
        assert queue.get_status("run")["status"] == "reviewing"
        assert queue._terminalize_wait_timeout("run") == ""
        assert queue.get_status("run")["status"] == "reviewing"
    finally:
        release.set()
        workers[0].join(5)
        assert not workers[0].is_alive()
    assert sent == []
    assert not (tmp_path / "cron/review-receipts.jsonl").exists()
    monkeypatch.setattr(queue, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(queue, "_owner_is_live", lambda *args: False)
    assert queue.recover_abandoned() == 1
    assert queue.get_status("run")["status"] == "pending"
    if expired:
        clock[0] += 61
    def replay(*args):
        assert not expired, "expired admission must skip review"
        return "PRESENT"
    monkeypatch.setattr(review, "_run_review_hop", replay)
    assert queue.drain(send) == 1
    workers[-1].join(5)
    assert not workers[-1].is_alive()
    assert queue.get_status("run")["status"] == "delivered"
    assert sent == ["original"]
    receipt = json.loads((tmp_path / "cron/review-receipts.jsonl").read_text())
    assert receipt["run_id"] == "run"
    assert receipt["decision"] == ("TIMEOUT_OR_ERROR" if expired else "PRESENT")
    assert receipt["text_sent"] == "original"
    assert receipt["message_id"] == "message"
    assert queue.drain(send) == 0


def test_restart_after_final_send_begins_records_unknown_without_replay(tmp_path, monkeypatch):
    from cron import delivery_queue as queue
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "cron/deliveries.db")
    queue.enqueue("run", {"id": "report", "review": {"subject": "topic"}}, "original")
    row = queue.claim_next()
    assert row["status"] == "reviewing"
    assert queue.begin_final_delivery("run", row["owner_process_id"])
    monkeypatch.setattr(queue, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(queue, "_owner_is_live", lambda *args: False)
    assert queue.recover_abandoned() == 1
    assert queue.get_status("run")["status"] == "unknown"
    assert queue.drain(lambda *args: pytest.fail("uncertain final must not replay")) == 0
    receipt = json.loads((tmp_path / "cron/review-receipts.jsonl").read_text())
    assert receipt["decision"] == "DELIVERY_UNKNOWN"
    assert receipt["run_id"] == "run"
    assert receipt["text_sent"] is None


def test_existing_queue_schema_expands_without_losing_claimed_or_pending_rows(tmp_path, monkeypatch):
    import sqlite3
    from cron import delivery_queue as queue
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    with sqlite3.connect(queue.queue_path()) as conn:
        conn.execute("""CREATE TABLE deliveries (
            execution_id TEXT PRIMARY KEY, job_json TEXT NOT NULL, content TEXT NOT NULL,
            for_failure INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK(status IN
                ('pending','delivering','delivered','failed','unknown','suppressed')),
            owner_process_id TEXT, owner_pid INTEGER, owner_started_at INTEGER,
            created_at TEXT NOT NULL, finished_at TEXT, error TEXT)""")
        for run, job, status in (("review", {"id": "report", "review": {"subject": "topic"}}, "pending"),
                                 ("send", {"id": "ordinary"}, "delivering")):
            conn.execute("INSERT INTO deliveries (execution_id, job_json, content, status, created_at) "
                         "VALUES (?, ?, 'original', ?, '2026-10-13T00:00:00+00:00')",
                         (run, json.dumps(job), status))
    assert queue.get_status("send")["status"] == "delivering"
    row = queue.claim_next()
    assert row["execution_id"] == "review"
    assert row["status"] == "reviewing"
    assert row["content"] == "original"
    assert queue.begin_final_delivery("review", row["owner_process_id"])
    assert queue._finish("review", error=None)
    assert queue.get_status("review")["status"] == "delivered"
    assert queue.get_status("send")["status"] == "delivering"
