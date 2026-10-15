"""Holding a reviewed report releases cron capacity and leaves ordinary delivery alone."""
from contextlib import nullcontext
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("reviewed", [False, True])
def test_save_compose_only_queues_reviewed_success(monkeypatch, reviewed):
    from cron import scheduler as sched
    from cron.execution_identity import enter_cron_execution, exit_cron_execution
    job = {"id": "job", "deliver": "local"}
    if reviewed:
        job["review"] = {"contract": "x", "subject": "y"}
    queued, sent, committed = Mock(), Mock(), Mock()
    monkeypatch.setattr("cron.delivery_queue.enqueue", queued)
    monkeypatch.setattr(sched, "save_job_output", lambda *a: None)
    monkeypatch.setattr(sched, "_is_interrupted", lambda *a: False)
    monkeypatch.setattr(sched, "_compose_run_delivery", lambda *a, **kw: ("report", False, False, False, None))
    monkeypatch.setattr(sched, "_deliver_result", sent)
    monkeypatch.setattr(sched, "_resolve_delivery_targets", lambda *a, **kw: [])
    token = enter_cron_execution(job, "run", {})
    fence = SimpleNamespace(side_effect_fence=lambda: nullcontext(True), lost=lambda: False)
    try:
        d = sched._RunDelivery(job=job, success=True, error=None)
        sched._save_compose_deliver(d, fence, "report", "output", adapters={}, loop=None,
                                   verbose=False, execution_token=None, commit_result=committed)
    finally:
        exit_cron_execution(token)
    committed.assert_called_once()
    assert queued.call_count == int(reviewed)
    assert sent.call_count == int(not reviewed)
    if reviewed:
        assert sched._run_delivery_outcome(d) == "queued"
        assert queued.call_args.args[1]["_review_run_id"] == "run"


def test_review_drain_does_not_block_ordinary_delivery(tmp_path, monkeypatch):
    from cron import delivery_queue as queue
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("review", {"id": "review", "review": {"subject": "topic"}}, "held")
    queue.enqueue("ordinary", {"id": "ordinary"}, "ordinary")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    sent = []
    def send(job, text, failure):
        if job["id"] == "review":
            started.set()
            assert release.wait(5)
            finished.set()
        sent.append(text)
    try:
        assert queue.drain(send) == 2
        assert started.wait(5)
        assert sent == ["ordinary"]
        assert queue.get_status("ordinary")["status"] == "delivered"
    finally:
        release.set()
        assert finished.wait(5)
    assert queue.drain(send) == 0


def test_local_job_without_review_uses_existing_delivery(monkeypatch):
    from cron.scheduler_delivery import _deliver_result
    monkeypatch.setattr("cron.scheduler_review._deliver_reviewed_result",
                        lambda *a: pytest.fail("non-opted-in job entered review"))
    assert _deliver_result({"id": "job", "deliver": "local"}, "original") is None


def test_silent_reviewed_run_records_receipt_without_queue_or_review(tmp_path, monkeypatch):
    import json
    from cron import scheduler as sched
    from cron.execution_identity import enter_cron_execution, exit_cron_execution
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = {"id": "job", "deliver": "local", "review": {"contract": "x", "subject": "topic"}}
    monkeypatch.setattr(sched, "save_job_output", lambda *a: None)
    monkeypatch.setattr(sched, "_is_interrupted", lambda *a: False)
    monkeypatch.setattr(sched, "_compose_run_delivery", lambda *a, **kw: ("[SILENT]", False, False, False, None))
    send, enqueue = Mock(), Mock()
    monkeypatch.setattr(sched, "_deliver_result", send)
    monkeypatch.setattr("cron.delivery_queue.enqueue", enqueue)
    token = enter_cron_execution(job, "run", {})
    fence = SimpleNamespace(side_effect_fence=lambda: nullcontext(True), lost=lambda: False)
    try:
        sched._save_compose_deliver(sched._RunDelivery(job=job, success=True, error=None), fence,
                                   "[SILENT]", "output", adapters={}, loop=None, verbose=False, execution_token=None)
    finally:
        exit_cron_execution(token)
    send.assert_not_called()
    enqueue.assert_not_called()
    receipt = json.loads((tmp_path / "cron/review-receipts.jsonl").read_text(encoding="utf-8-sig"))
    assert receipt["decision"] == "SILENT"
    assert receipt["run_id"] == "run"
