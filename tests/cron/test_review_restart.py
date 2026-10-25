"""Only final sends are uncertain across a gateway restart, never held reviews."""
import json
import threading
import time
from types import SimpleNamespace

import pytest


def test_restart_inside_review_hop_sends_original_without_a_second_prompt(tmp_path, monkeypatch):
    from cron import delivery_queue as queue, scheduler_review as review

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "cron/deliveries.db")
    monkeypatch.setattr(review, "_shutdown", threading.Event())
    job = {"id": "report", "deliver": "local", "_review_run_id": "run",
           "_review_deadline_epoch": time.time() + 600,
           "review": {"subject": "topic", "contract": "report"}}
    subject = {"session_key": "key", "session_id": "sid", "chat_id": "42",
               "thread_id": "123", "user_id": "42"}
    monkeypatch.setattr(review, "_resolve_review_subject", lambda _: subject)
    monkeypatch.setattr("cron.review.load_contract", lambda _: {})
    monkeypatch.setattr("gateway.host_attach.host_gateway_serving", lambda *args: None)
    prompts = []
    entered, release = threading.Event(), threading.Event()
    def control(home, method, payload=None, params=None, timeout=None):
        if method == "cron-review":
            prompts.append(payload["text"])
            return {"token": "t"}
        entered.set()
        assert release.wait(5)
        return {"status": "pending"}
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", control)
    workers = []
    thread = threading.Thread
    def spawn(*args, **kwargs):
        workers.append(thread(*args, **kwargs))
        return workers[-1]
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
        review.stop_reviews()
    finally:
        release.set()
        workers[0].join(5)
        assert not workers[0].is_alive()
    assert sent == []
    assert queue.get_status("run")["status"] == "reviewing"
    assert not (tmp_path / "cron/review-receipts.jsonl").exists()
    monkeypatch.setattr(review, "_shutdown", threading.Event())
    monkeypatch.setattr(queue, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(queue, "_owner_is_live", lambda *args: False)
    assert queue.recover_abandoned() == 1
    assert queue.get_status("run")["status"] == "pending"
    assert queue.drain(send) == 1
    workers[-1].join(5)
    assert len(prompts) == 1
    assert not workers[-1].is_alive()
    assert sent == ["original"]
    assert queue.get_status("run")["status"] == "delivered"
    receipt = json.loads((tmp_path / "cron/review-receipts.jsonl").read_text())
    assert receipt["run_id"] == "run"
    assert receipt["decision"] == "TIMEOUT_OR_ERROR"
    assert receipt["replayed"] is True
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


def test_restart_during_rerun_spends_at_most_one_rerun_and_one_review_prompt(tmp_path, monkeypatch):
    from cron import delivery_queue as queue, scheduler_review as review

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "cron/deliveries.db")
    monkeypatch.setattr(review, "_shutdown", threading.Event())
    job = {"id": "report", "deliver": "local", "_review_run_id": "run",
           "_review_deadline_epoch": time.time() + 600,
           "review": {"subject": "topic", "contract": "report"}}
    subject = {"session_key": "key", "session_id": "sid", "chat_id": "42",
               "thread_id": "123", "user_id": "42"}
    monkeypatch.setattr(review, "_resolve_review_subject", lambda _: subject)
    monkeypatch.setattr("cron.review.load_contract", lambda _: {})
    prompts, reruns = [], []
    def ask(*args):
        prompts.append(args)
        return "RERUN\nstale numbers"
    monkeypatch.setattr(review, "_run_review_hop", ask)
    entered, release = threading.Event(), threading.Event()
    def rerun(job, remaining):
        reruns.append(job)
        from contextlib import closing
        with closing(queue._connect()) as conn:
            stored = json.loads(conn.execute(
                "SELECT job_json FROM deliveries WHERE execution_id='run'").fetchone()[0])
        assert stored["_review_rerun_attempted"] is True
        entered.set()
        assert release.wait(5)
        raise review.ReviewAbandoned("gateway stopped during rerun")
    monkeypatch.setattr(review, "_bounded_review_rerun", rerun)
    workers = []
    thread = threading.Thread
    def spawn(*args, **kwargs):
        workers.append(thread(*args, **kwargs))
        return workers[-1]
    monkeypatch.setattr(queue.threading, "Thread", spawn)
    sent = []
    def final_send(job, text, **kwargs):
        sent.append(text)
    monkeypatch.setattr("cron.scheduler_delivery._deliver_result", final_send)
    send = lambda job, text, failure: review._deliver_reviewed_result(job, text, {}, None)
    queue.enqueue("run", job, "original")
    try:
        assert queue.drain(send) == 1
        assert entered.wait(5)
    finally:
        release.set()
        workers[0].join(5)
    assert sent == []
    monkeypatch.setattr(queue, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(queue, "_owner_is_live", lambda *args: False)
    assert queue.recover_abandoned() == 1
    assert queue.drain(send) == 1
    workers[-1].join(5)
    assert len(prompts) == 1
    assert len(reruns) == 1
    assert sent == ["original"]
    assert queue.get_status("run")["status"] == "delivered"
    receipt = json.loads((tmp_path / "cron/review-receipts.jsonl").read_text())
    assert receipt["decision"] == "RERUN_UNAVAILABLE"
    assert receipt["replayed"] is True


def test_unknown_receipt_failure_never_stalls_ordinary_deliveries(tmp_path, monkeypatch):
    from cron import delivery_queue as queue
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "cron/deliveries.db")
    queue.enqueue("run", {"id": "report", "review": {"subject": "topic"}}, "original")
    row = queue.claim_next()
    assert queue.begin_final_delivery("run", row["owner_process_id"])
    monkeypatch.setattr(queue, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(queue, "_owner_is_live", lambda *args: False)
    def broken(_):
        raise TimeoutError("Timed out waiting for cron review receipts")
    monkeypatch.setattr("cron.review.append_receipt", broken)
    queue.enqueue("notice", {"id": "ordinary"}, "hello")
    sent = []
    assert queue.drain(lambda job, text, failure: sent.append(text)) == 1
    assert sent == ["hello"]
    assert queue.get_status("notice")["status"] == "delivered"
    assert queue.get_status("run")["status"] == "unknown"


def test_stopping_gateway_leaves_held_reports_pending(tmp_path, monkeypatch):
    from cron import delivery_queue as queue, scheduler_review as review
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "cron/deliveries.db")
    monkeypatch.setattr(review, "_shutdown", threading.Event())
    queue.enqueue("run", {"id": "report", "review": {"subject": "topic"}}, "original")
    queue.enqueue("notice", {"id": "ordinary"}, "hello")
    review.stop_reviews()
    assert queue.claim_next()["execution_id"] == "notice"
    assert queue.claim_next() is None
    assert queue.get_status("run")["status"] == "pending"


def test_report_replayed_after_its_deadline_goes_out_without_review(tmp_path, monkeypatch):
    from cron import delivery_queue as queue, scheduler_review as review
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "cron/deliveries.db")
    monkeypatch.setattr(review, "_shutdown", threading.Event())
    subject = {"session_key": "key", "session_id": "sid", "chat_id": "42",
               "thread_id": "123", "user_id": "42"}
    monkeypatch.setattr(review, "_resolve_review_subject", lambda _: subject)
    monkeypatch.setattr("cron.review.load_contract", lambda _: {})
    monkeypatch.setattr(review, "_run_review_hop", lambda *args: pytest.fail("expired admission must skip review"))
    sent = []
    monkeypatch.setattr("cron.scheduler_delivery._deliver_result", lambda job, text, **kwargs: sent.append(text))
    job = {"id": "report", "deliver": "local", "_review_run_id": "run",
           "_review_deadline_epoch": time.time() - 1, "review": {"subject": "topic", "contract": "report"}}
    assert review._deliver_reviewed_result(job, "original", {}, None) is None
    assert sent == ["original"]
    receipt = json.loads((tmp_path / "cron/review-receipts.jsonl").read_text())
    assert receipt["decision"] == "TIMEOUT_OR_ERROR"
