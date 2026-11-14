"""A failed child of a still-running detached fan-out is surfaced to the parent immediately.

Batches join on the slowest sibling before ONE consolidated block re-enters. In a 1,393-agent run every wave-1
child died in a 401 storm at 08:29 and the parent learned of it at 09:36 from the batch's "unknown outcome"
block: 66 minutes of a dead wave with nothing running.
"""
import queue
import threading
from unittest.mock import patch

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification


def _drain_completion_queue():
    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except queue.Empty:
            return


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    _drain_completion_queue()
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()
    _drain_completion_queue()


def _record(status="running"):
    return {"delegation_id": "deleg_x", "status": status, "is_batch": True, "goals": ["a", "b", "c"], "goal": "a",
            "session_key": "sk", "origin_ui_session_id": "ui", "origin_session_id": "", "parent_session_id": "root",
            "dispatched_at": 1.0, "role": "leaf", "model": "m", "context": None, "toolsets": None}


def _real_running_batch(goals):
    gate = threading.Event()
    def runner():
        gate.wait(5)
        return {"results": []}
    handle = ad.dispatch_async_delegation_batch(
        goals=goals, context=None, toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:1", parent_session_id="root",
        runner=runner, max_async_children=1,
    )
    assert handle["status"] == "dispatched"
    delegation_id = handle["delegation_id"]
    assert ad._records[delegation_id]["status"] == "running"
    return delegation_id, gate




@pytest.mark.parametrize("retry_attempt,root_age", [(0, 0), (6, 0), (0, 24 * 3600 + 1)])
def test_real_dispatched_multi_child_terminal_notice_uses_persisted_retry_row(retry_attempt, root_age):
    """A transient child in a real persisted multi-goal row is terminal, not silently auto-retried."""
    from tools import delegation_resume as dr

    delegation_id, gate = _real_running_batch(["a", "b"])
    entry = {"task_index": 0, "status": "failed", "failure_reason": "rate_limit", "error": "429"}
    assert ad.record_unit_child(delegation_id, entry) is None
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET retry_attempt=?, retry_root_started_at=? WHERE delegation_id=?",
                     (retry_attempt, ad._records[delegation_id]["dispatched_at"] - root_age, delegation_id))
    q = queue.Queue()
    with patch("tools.process_registry.process_registry") as reg:
        reg.completion_queue = q
        ad.push_task_failure_notice(delegation_id, entry, n_tasks=2)
        assert q.qsize() == 1
        gate.set()
        ad._records[delegation_id]["_future"].result(timeout=5)
    persisted = dr.retry_status(delegation_id)
    assert persisted["retry_state"] == dr.RETRY_TERMINAL


def test_real_dispatched_scalar_transient_notice_is_suppressed_only_when_classifier_retries():
    delegation_id, gate = _real_running_batch(["a"])
    entry = {"task_index": 0, "status": "failed", "failure_reason": "rate_limit", "error": "429"}
    ad.record_unit_child(delegation_id, entry)
    q = queue.Queue()
    with patch("tools.process_registry.process_registry") as reg:
        reg.completion_queue = q
        ad.push_task_failure_notice(delegation_id, entry, n_tasks=1)
        assert q.empty()
        gate.set()
        ad._records[delegation_id]["_future"].result(timeout=5)
    from tools import delegation_resume as dr
    assert dr.retry_status(delegation_id)["retry_state"] == dr.RETRY_SCHEDULED


def test_failure_notice_reaches_the_queue_while_the_batch_keeps_running_and_formats_as_early_warning():
    q = queue.Queue()
    entry = {"task_index": 1, "status": "error", "error": "401 authentication_error: key invalid", "duration_seconds": 12.5,
             "live_transcript": "/tmp/live/task-1.log"}
    with patch.object(ad, "_records", {"deleg_x": _record()}), \
         patch("tools.process_registry.process_registry") as reg:
        reg.completion_queue = q
        ad.push_task_failure_notice("deleg_x", entry, n_tasks=3)
        assert ad._records["deleg_x"]["status"] == "running"  # not finalized
    evt = q.get_nowait()
    assert evt["type"] == "async_delegation" and evt["task_failure_notice"] is True
    assert (evt["session_key"], evt["origin_ui_session_id"], evt["parent_session_id"]) == ("sk", "ui", "root")
    text = format_process_notification(evt)
    assert text.startswith("[ASYNC DELEGATION TASK FAILED — deleg_x, task 2/3]")
    assert "Task: b" in text and "401 authentication_error" in text and "/tmp/live/task-1.log" in text
    assert "consolidated results will still arrive" in text


def test_failure_notice_is_durable_and_does_not_claim_the_terminal_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record = _record()
    ad._persist_dispatch(record)
    with patch.object(ad, "_records", {"deleg_x": record}), \
         patch("tools.process_registry.process_registry") as reg:
        reg.completion_queue = queue.Queue()
        ad.push_task_failure_notice("deleg_x", {"task_index": 1, "status": "error", "error": "401"}, n_tasks=3)
        notice = reg.completion_queue.get_nowait()
    assert notice["_delivery_event_id"] == "deleg_x:task-failure:1"
    claim = ad.claim_event_delivery(notice, "tui-poller")
    assert claim and claim.startswith("outbox:")
    ad.complete_event_delivery(notice, claim)
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute(
            "SELECT delivery_state FROM async_delegation_events WHERE event_id=?",
            (notice["_delivery_event_id"],),
        ).fetchone() == ("delivered",)
        assert conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            ("deleg_x",),
        ).fetchone() == ("pending",)


def test_terminal_write_failure_keeps_completion_replayable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    original = ad._persist_completion
    monkeypatch.setattr(ad, "_persist_completion", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    result = ad.dispatch_async_delegation(
        goal="fallback", context=None, toolsets=None, role="leaf", model="m", session_key="owner",
        runner=lambda: {"status": "completed", "summary": "kept"}, max_async_children=1,
    )
    event = process_registry.completion_queue.get(timeout=5)
    assert event["summary"] == "kept"
    assert event["_delivery_event_id"] == f"{result['delegation_id']}:terminal_fallback"
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    monkeypatch.setattr(ad, "_owner_liveness", lambda: (lambda pid, started: False))
    assert ad.restore_undelivered_completions(process_registry.completion_queue) == 1
    replayed = process_registry.completion_queue.get_nowait()
    assert replayed["delegation_id"] == result["delegation_id"]
    assert replayed["summary"] == "kept"
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute(
            "SELECT state, event_json FROM async_delegations WHERE delegation_id=?",
            (result["delegation_id"],),
        ).fetchone() == ("completed", None)
    from tools.delegation_resume import list_boot_candidates
    assert list_boot_candidates() == []
    monkeypatch.setattr(ad, "_persist_completion", original)


def test_notice_is_not_sent_for_a_finished_batch():
    q = queue.Queue()
    with patch.object(ad, "_records", {"deleg_x": _record(status="completed")}), \
         patch("tools.process_registry.process_registry") as reg:
        reg.completion_queue = q
        ad.push_task_failure_notice("deleg_x", {"task_index": 0, "status": "error"}, n_tasks=3)
    assert q.empty()


def test_interim_notice_claims_its_outbox_row_without_acknowledging_the_batch_final_row(tmp_path, monkeypatch):
    """A durable notice owns an outbox row distinct from the batch's terminal lifecycle row."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    record = _record()
    ad._persist_dispatch(record)
    notice = {"type": "async_delegation", "delegation_id": "deleg_x", "task_failure_notice": True,
              "results": [{"task_index": 0}]}
    ad._persist_outbox_event(notice, None, event_kind="task_failure")
    final = {"type": "async_delegation", "delegation_id": "deleg_x", "is_batch": True, "results": []}
    claim = ad.claim_event_delivery(notice, "tui-poller")
    assert claim and claim.startswith("outbox:")
    ad.complete_event_delivery(notice, claim)
    assert ad.is_interim_delegation_event(notice) and not ad.is_interim_delegation_event(final)
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute(
            "SELECT delivery_state FROM async_delegation_events WHERE event_id=?",
            (notice["_delivery_event_id"],),
        ).fetchone() == ("delivered",)
        assert conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            ("deleg_x",),
        ).fetchone() == ("pending",)


def test_gateway_dedup_identity_separates_notices_from_the_final_and_from_each_other():
    from gateway.run_notifications import GatewayNotificationsMixin
    ident = GatewayNotificationsMixin._completion_delivery_identity
    n0 = {"type": "async_delegation", "delegation_id": "d", "task_failure_notice": True, "results": [{"task_index": 0}]}
    n1 = {"type": "async_delegation", "delegation_id": "d", "task_failure_notice": True, "results": [{"task_index": 1}]}
    final = {"type": "async_delegation", "delegation_id": "d", "is_batch": True, "results": []}
    no_index_a = {"type": "async_delegation", "delegation_id": "d", "task_failure_notice": True,
                  "results": [{"status": "error", "error": "a"}]}
    no_index_b = {"type": "async_delegation", "delegation_id": "d", "task_failure_notice": True,
                  "results": [{"status": "error", "error": "b"}]}
    assert len({ident(n0), ident(n1), ident(final)}) == 3
    assert ad._outbox_event_id(no_index_a, "task_failure") != ad._outbox_event_id(no_index_b, "task_failure")
