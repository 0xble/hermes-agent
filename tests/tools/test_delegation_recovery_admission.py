"""Recovery must use trusted restart cuts and durable admission on every route."""

import json
import queue
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import async_delegation as ad
from tools import delegate_tool
from tools import delegate_tool_dispatch as dispatch
from tools import delegation_resume as dr
from tools.delegate_tool_registry import _handle_control_action
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification
from tests.tools.test_delegation_resume_children import _Parent, _batch_row


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


@pytest.mark.parametrize("reason", ["gateway shutdown", "gateway shutdown (final-cleanup)", "/stop",
                                    "gateway shutdown (restart)"])
def test_gateway_cut_notice_and_recovery_follow_restart_authority(reason):
    started, release = threading.Event(), threading.Event()

    def run():
        started.set()
        assert release.wait(5)
        return {"results": [
            {"task_index": 0, "status": "completed", "summary": "done"},
            {"task_index": 1, "status": "interrupted"},
        ]}

    handle = ad.dispatch_async_delegation_batch(
        goals=["first", "second"], context="ctx", toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:1", parent_session_id=_Parent.session_id,
        runner=run, interrupt_fn=lambda reason=None: release.set(), max_async_children=1,
    )
    did = handle["delegation_id"]
    assert handle["status"] == "dispatched"
    assert started.wait(5)
    try:
        assert dr.retry_status(did)["retry_state"] == "armed"
        assert ad.interrupt_all(reason) == 1
        deadline = time.monotonic() + 5
        event = None
        while time.monotonic() < deadline:
            try:
                candidate = process_registry.completion_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if candidate.get("delegation_id") == did:
                event = candidate
                break
        assert event is not None
        assert event["cut_reason"] == reason
        assert dr.retry_status(did)["state"] == "interrupted"
        text = format_process_notification(event)
        if reason == "gateway shutdown (restart)":
            assert "action='resume'" in text
            claim = ad.claim_event_delivery(event, "test")
            assert ad.complete_completion_delivery(did, claim)
            assert dr.sweep_retries(now=time.time() + dr.NOTICE_GRACE_S * 3) == []
            payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
            assert payload["status"] == "retry_claimed"
            assert [task["goal"] for task in payload["tasks"]] == ["second"]
            assert "verify current state" in payload["tasks"][0]["context"]
            return
        assert "no continuation was requested" in text
        assert "action='resume'" not in text
        assert "retry this automatically" not in text
        assert dr.retry_status(did)["retry_state"] not in dr.RETRY_PENDING_STATES
        assert dr.sweep_retries(now=time.time() + dr.NOTICE_GRACE_S * 3) == []
        payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
        assert "error" in payload
        assert "tasks" not in payload and "recovery_context" not in payload
    finally:
        release.set()


@pytest.mark.parametrize("metadata", ["event", "result"])
@pytest.mark.parametrize("reason", ["gateway shutdown", "gateway shutdown (final-cleanup)"])
def test_nonrestart_cut_classification_is_terminal(metadata, reason):
    record = dict(state="interrupted", task={"goals": ["first", "second"]},
                  result={"results": [{"task_index": 0, "status": "completed"}]},
                  event={}, parent_session_id="owner", origin_session_id="")
    record[metadata]["cut_reason"] = reason
    assert dr.classify_outcome(record)[0] == dr.RETRY_NONE


def _legacy_batch(monkeypatch, *, shape="tasks"):
    did = _batch_row("deleg_legacy_storage_cut")
    payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
    assert payload["status"] == "recovery_claimed"
    task = dict(payload["tasks"][0])
    brief = task.pop("context") if shape == "legacy" else None
    batch = SimpleNamespace(
        task_list=[task], context=brief, children=[(0, task, object())],
        parent_agent=_Parent(), creds={"model": "m"}, top_role="leaf", live_writers=[],
        live_paths=[], live_deleg_id="deleg_legacy_storage_replacement", origin_wake_sid="wake",
        origin_session_history_delivery=True, origin_ui_session_id="tui-tab",
    )
    monkeypatch.setattr(dispatch, "_resolve_async_session_key", lambda *_: ("telegram:dm:1", "tui-tab"))
    monkeypatch.setattr(dispatch, "_units_of", lambda b: [b])
    monkeypatch.setattr(dispatch, "_detach_child", Mock())
    monkeypatch.setattr(dispatch, "_cleanup_unit", Mock())
    monkeypatch.setattr(dispatch, "_run_sync_with_note", Mock(return_value='{"status":"completed"}'))
    monkeypatch.setattr(dispatch, "_execute_and_aggregate", Mock(return_value={"results": []}))
    monkeypatch.setattr(delegate_tool, "_get_max_async_children", lambda: 0)
    monkeypatch.setattr(delegate_tool, "_get_max_queued_delegations", lambda: 1)
    return did, batch, payload["tasks"][0]


@pytest.mark.parametrize("shape", ["tasks", "legacy"])
def test_legacy_child_persist_failure_never_runs_inline_or_consumes_claim(monkeypatch, shape):
    did, batch, task = _legacy_batch(monkeypatch, shape=shape)
    original_persist = ad._persist_dispatch

    def fail_insert(record):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as m:
        m.setattr(ad, "_persist_dispatch", fail_insert)
        result = json.loads(dispatch._dispatch_background(batch))
    assert result["status"] == "rejected"
    dispatch._run_sync_with_note.assert_not_called()
    dispatch._execute_and_aggregate.assert_not_called()
    assert dr.retry_status(batch.live_deleg_id) is None
    assert dr.retry_status(did)["retry_state"] == "none"
    assert "will be retried later" not in result["error"]  # TUI has no automatic retry driver
    with ad._DB_LOCK, ad._transaction() as conn:
        receipts = conn.execute("SELECT task_index, replacement_delegation_id FROM "
                                "async_delegation_child_recovery WHERE delegation_id=? ORDER BY task_index",
                                (did,)).fetchall()
    assert receipts == [(1, None), (2, None)]
    replacement = dict(delegation_id=batch.live_deleg_id, goal=task["goal"], context=task["context"],
                       session_key="telegram:dm:1", parent_session_id=_Parent.session_id,
                       origin_session_id=_Parent.session_id, dispatched_at=time.time())
    original_persist(replacement)
    with pytest.raises(dr.RetryAdmissionError, match="consumed"):
        original_persist(dict(replacement, delegation_id="deleg_legacy_duplicate"))
    with ad._DB_LOCK, ad._transaction() as conn:
        receipts = conn.execute("SELECT task_index, replacement_delegation_id FROM "
                                "async_delegation_child_recovery WHERE delegation_id=? ORDER BY task_index",
                                (did,)).fetchall()
    assert receipts == [(1, batch.live_deleg_id), (2, None)]


@pytest.mark.parametrize("metadata", ["event", "result"])
def test_nonrestart_rows_from_older_scheduler_are_cancelled_without_a_notice(metadata):
    did = _batch_row("deleg_old_shutdown_plan")
    record = dr.retry_status(did)
    value = dict(record[metadata], cut_reason="gateway shutdown")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(f"UPDATE async_delegations SET {metadata}_json=?, retry_state='scheduled', "
                     "retry_due_at=?, delivery_state='delivered' WHERE delegation_id=?",
                     (json.dumps(value), time.time() - 1, did))
    assert dr.claim_retry_dispatch(did)[1] is not None
    assert dr.retry_note(dict(delegation_id=did, retry_state="scheduled", cut_reason="gateway shutdown")) == ""
    assert dr.sweep_retries(now=time.time() + dr.NOTICE_GRACE_S) == []
    assert dr.retry_status(did)["retry_state"] == "cancelled"
    assert "error" in json.loads(_handle_control_action("resume", did, None, _Parent()))


@pytest.mark.parametrize("reason", ["/stop", "gateway shutdown"])
def test_stop_after_legacy_claim_cannot_admit_a_replacement(monkeypatch, reason):
    did, batch, task = _legacy_batch(monkeypatch)
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET event_json=? WHERE delegation_id=?",
                     (json.dumps({"cut_reason": reason}), did))
    with pytest.raises(dr.RetryAdmissionError, match="stopped"):
        ad._persist_dispatch(dict(
            delegation_id=batch.live_deleg_id, goal=task["goal"], context=task["context"],
            session_key="telegram:dm:1", parent_session_id=_Parent.session_id,
            origin_session_id=_Parent.session_id, dispatched_at=time.time(),
        ))
    assert dr.retry_status(batch.live_deleg_id) is None


@pytest.mark.parametrize("route", ["no_async", "synchronous"])
@pytest.mark.parametrize("shape", ["tasks", "legacy"])
def test_recovery_cannot_bypass_atomic_admission_on_sync_routes(monkeypatch, route, shape):
    did, batch, _ = _legacy_batch(monkeypatch, shape=shape)
    if route == "no_async":
        monkeypatch.setattr(dispatch, "_resolve_async_wake_sid", lambda *_: None)
        result = json.loads(dispatch._dispatch_background(batch))
    else:
        result = json.loads(dispatch._run_batch(batch, background=False))
    assert result["status"] == "rejected"
    assert "durable" in result["error"].lower()
    dispatch._run_sync_with_note.assert_not_called()
    dispatch._execute_and_aggregate.assert_not_called()
    assert dr.retry_status(batch.live_deleg_id) is None
    assert dr.retry_status(did)["resume_attempts"] == 1
