"""A failed child of a still-running detached fan-out is surfaced to the parent immediately.

Batches join on the slowest sibling before ONE consolidated block re-enters. In a 1,393-agent run every wave-1
child died in a 401 storm at 08:29 and the parent learned of it at 09:36 from the batch's "unknown outcome"
block: 66 minutes of a dead wave with nothing running.
"""
import queue
from unittest.mock import patch

from tools import async_delegation as ad
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification
from tui_gateway.session_notifications import _notification_event_dedup_key


def _record(status="running"):
    return {"delegation_id": "deleg_x", "status": status, "is_batch": True, "goals": ["a", "b", "c"], "goal": "a",
            "session_key": "sk", "origin_ui_session_id": "ui", "origin_session_id": "", "parent_session_id": "root",
            "dispatched_at": 1.0, "role": "leaf", "model": "m", "context": None, "toolsets": None}


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
    assert ad.restore_undelivered_completions(process_registry.completion_queue) == 1
    replayed = process_registry.completion_queue.get_nowait()
    assert replayed["delegation_id"] == result["delegation_id"]
    assert replayed["summary"] == "kept"
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
    assert len({ident(n0), ident(n1), ident(final)}) == 3
