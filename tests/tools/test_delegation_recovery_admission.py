"""Recovery must use explicit user-stop evidence and durable admission on every route."""

import json
import queue
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from tools import async_delegation as ad
from tools import async_delegation_completion as execution_checkpoints
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
        if reason == "/stop":
            assert "no continuation was requested" in text
            assert "action='resume'" not in text
            assert "retry this automatically" not in text
            assert dr.retry_status(did)["retry_state"] not in dr.RETRY_PENDING_STATES
            assert dr.sweep_retries(now=time.time() + dr.NOTICE_GRACE_S * 3) == []
            payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
            assert "error" in payload
            assert "tasks" not in payload and "recovery_context" not in payload
            return
        assert "no continuation was requested" not in text
        # One instruction per notice: resume for a restart cut, automatic retry otherwise.
        auto = "retry this automatically" in text
        assert ("action='resume'" in text) is (reason == "gateway shutdown (restart)")
        assert auto is (reason != "gateway shutdown (restart)")
        if reason == "gateway shutdown (restart)":
            claim = ad.claim_event_delivery(event, "test")
            assert ad.complete_completion_delivery(did, claim)
            assert dr.sweep_retries(now=time.time() + dr.NOTICE_GRACE_S * 3) == []
            payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
            assert payload["status"] == "retry_claimed"
            assert [task["goal"] for task in payload["tasks"]] == ["second"]
            assert "verify current state" in payload["tasks"][0]["context"]
            return
        assert dr.retry_status(did)["retry_state"] in dr.RETRY_PENDING_STATES
    finally:
        release.set()


@pytest.mark.parametrize("metadata", ["event", "result"])
@pytest.mark.parametrize("reason", ["gateway shutdown", "gateway shutdown (final-cleanup)",
                                    "gateway shutdown (post-interrupt)"])
def test_nonrestart_cut_classification_is_recoverable(metadata, reason):
    record = dict(state="interrupted", task={"goals": ["first", "second"]},
                  result={"results": [{"task_index": 0, "status": "completed"}]},
                  event={}, parent_session_id="owner", origin_session_id="")
    record[metadata]["cut_reason"] = reason
    assert dr.classify_outcome(record)[0] == "retry"


def _legacy_batch(monkeypatch, *, shape="tasks"):
    did = _batch_row("deleg_legacy_storage_cut")
    payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
    assert payload["status"] == "recovery_claimed"
    task = dict(payload["tasks"][0])
    brief = task.pop("context") if shape == "legacy" else None
    batch = dispatch._Batch(
        max_children=1, origin_owner_transport=None, origin_owner_session_record=None,
                            overall_start=time.monotonic(), task_list=[task], context=brief, children=[(0, task, object())],
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
    did, batch, _task = _legacy_batch(monkeypatch, shape=shape)
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
    assert receipts == []
    retry_payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
    assert retry_payload["status"] == "recovery_claimed"
    retry_task = retry_payload["tasks"][0]
    replacement = dict(delegation_id=batch.live_deleg_id, goal=retry_task["goal"], context=retry_task["context"],
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
def test_nonrestart_rows_from_older_scheduler_remain_retryable(metadata):
    did = _batch_row("deleg_old_shutdown_plan")
    record = dr.retry_status(did)
    value = dict(record[metadata], cut_reason="gateway shutdown (post-interrupt)")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(f"UPDATE async_delegations SET {metadata}_json=?, retry_state='scheduled', "
                     "retry_due_at=?, delivery_state='delivered' WHERE delegation_id=?",
                     (json.dumps(value), time.time() - 1, did))
    assert dr.claim_retry_dispatch(did)[1] is None
    note = dr.retry_note(dict(delegation_id=did, retry_state="scheduled",
                              cut_reason="gateway shutdown (post-interrupt)",
                              due_at=time.time(), attempt=1, reason="interrupted"))
    assert "retry this automatically" in note
    actions = dr.sweep_retries(now=time.time() + dr.NOTICE_GRACE_S)
    assert len(actions) == 1 and actions[0]["kind"] == "notice"
    assert dr.retry_status(did)["retry_state"] == "noticing"


def test_sweep_records_user_stop_reason_when_revoking_owed_retry():
    did = _batch_row("deleg_sweep_user_stop")
    record = dr.retry_status(did)
    event = dict(record["event"], cut_reason="/stop")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET state='interrupted', event_json=?, retry_state='scheduled', "
                     "retry_due_at=?, delivery_state='delivered' WHERE delegation_id=?",
                     (json.dumps(event), time.time() - 1, did))
    assert dr.sweep_retries(now=time.time() + dr.NOTICE_GRACE_S) == []
    row = dr.retry_status(did)
    assert row["retry_state"] == "cancelled"
    assert row["retry_reason"] == "stopped by user"


@pytest.mark.parametrize("reason", ["/stop"])
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


@pytest.mark.parametrize("route,phase", [("schedule", "armed"), ("sweep", "armed"),
                                         ("sweep", "scheduled"), ("sweep", "dispatching")])
def test_execution_seal_prevents_retry_sweeper_claims_and_notices(route, phase):
    now = time.time()

    def row(did):
        with sqlite3.connect(ad._db_path()) as conn:
            return conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?", (did,)).fetchone()

    def prepare(did, *, sealed):
        _batch_row(did)
        with ad._DB_LOCK, ad._transaction() as conn:
            task = json.loads(conn.execute("SELECT task_json FROM async_delegations WHERE delegation_id=?",
                                          (did,)).fetchone()[0])
            if sealed:
                task["execution_sealed_at"] = now
            conn.execute("UPDATE async_delegations SET task_json=?, retry_state=?, retry_due_at=?, "
                         "retry_claim=?, retry_claimed_at=?, delivery_state='delivered' WHERE delegation_id=?",
                         (json.dumps(task), phase, now - 60,
                          "stale-dispatch-claim" if phase == "dispatching" else None,
                          now - dr.RETRY_CLAIM_TTL_S - dr.NOTICE_GRACE_S - 1, did))

    def advance(did):
        return dr.schedule_retry(did, now=now) if route == "schedule" else dr.sweep_retries(now=now)

    control, sealed = "deleg_unsealed_sweep_control", "deleg_sealed_retry_sweep"
    prepare(control, sealed=False)
    control_before = row(control)
    advance(control)
    assert row(control) != control_before, "the unsealed control must exercise real due retry work"
    prepare(sealed, sealed=True)
    before = row(sealed)
    actions = advance(sealed)
    assert row(sealed) == before, "the retry sweeper must not classify, expire or claim sealed execution"
    assert not actions, "sealed execution must create neither recovery notices nor terminal reports"
    with sqlite3.connect(ad._db_path()) as conn:
        assert conn.execute("SELECT * FROM async_delegation_child_recovery WHERE delegation_id=?",
                            (sealed,)).fetchall() == []


@pytest.mark.parametrize("shape", ["scalar", "batch"])
@pytest.mark.parametrize("route", ["manual", "retry", "available"])
def test_execution_seal_refuses_new_recovery_claim_without_mutating_ledger(shape, route):
    did = _batch_row("deleg_sealed_new_claim")
    with ad._DB_LOCK, ad._transaction() as conn:
        task = json.loads(conn.execute("SELECT task_json FROM async_delegations WHERE delegation_id=?",
                                      (did,)).fetchone()[0])
        if shape == "scalar":
            task.update(goal="unfinished scalar", goals=["unfinished scalar"], task_indexes=None)
            conn.execute("UPDATE async_delegations SET result_json=? WHERE delegation_id=?",
                         (json.dumps({"results": [{"task_index": 0, "status": "interrupted"}]}), did))
        conn.execute("UPDATE async_delegations SET task_json=?, retry_state=?, retry_due_at=? "
                     "WHERE delegation_id=?", (json.dumps(task), "scheduled" if route == "retry" else "none",
                                               time.time(), did))
    assert dr.resume_available(did), "the unsealed fixture must be genuinely eligible"
    with ad._DB_LOCK, ad._transaction() as conn:
        task["execution_sealed_at"] = time.time()
        conn.execute("UPDATE async_delegations SET task_json=? WHERE delegation_id=?", (json.dumps(task), did))

    def stored_rows():
        with sqlite3.connect(ad._db_path()) as conn:
            return (conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?", (did,)).fetchall(),
                    conn.execute("SELECT * FROM async_delegation_child_recovery WHERE delegation_id=?", (did,)).fetchall())

    before = stored_rows()
    if route == "available":
        assert not dr.resume_available(did), "a sealed execution cannot advertise another recovery"
    else:
        claim = dr.claim_retry_dispatch if route == "retry" else (
            dr.claim_resume if shape == "scalar" else dr.claim_batch_resume)
        record, reason = claim(did)
        assert record is not None
        assert reason == "execution_sealed", "a seal must reject new scalar, child and automatic recovery claims"
    assert stored_rows() == before, "refusing a sealed execution must not create or consume a claim"


@pytest.mark.parametrize("shape", ["tasks", "legacy"])
def test_sealed_execution_refuses_previously_claimed_recovery(monkeypatch, shape):
    from typing import cast

    did, batch, _ = _legacy_batch(monkeypatch, shape=shape)
    # A native seal may arrive after a caller takes its recovery brief. This
    # admission fixture represents stopped executions, not successful outcomes.
    with ad._DB_LOCK, ad._transaction() as conn:
        row = conn.execute("SELECT task_json FROM async_delegations WHERE delegation_id=?", (did,)).fetchone()
        task = json.loads(row[0])
        finished_at = time.time()
        task["child_execution"] = {
            str(index): {"child_session_id": f"{did}-child-{index}",
                         "checkpoint": "finished_child", "finished_at": finished_at}
            for index in range(len(task["goals"]))
        }
        task["unit_execution"] = {"checkpoint": "finished_unit", "finished_at": finished_at}
        task["execution_sealed_at"] = finished_at
        conn.execute("UPDATE async_delegations SET task_json=? WHERE delegation_id=?", (json.dumps(task), did))
    source_before = dr.retry_status(did)
    assert source_before is not None and source_before["resume_attempts"] == 1
    result = json.loads(dispatch._dispatch_background(cast(dispatch._Batch, batch)))
    assert result["status"] == "rejected", "a previously claimed brief cannot reopen sealed execution"
    assert "sealed" in result["error"].lower()
    cast(Mock, dispatch._run_sync_with_note).assert_not_called()
    cast(Mock, dispatch._execute_and_aggregate).assert_not_called()
    assert dr.retry_status(batch.live_deleg_id) is None
    assert dr.retry_status(did) == source_before, "policy rejection must not reopen or consume the original claim"
    with sqlite3.connect(ad._db_path()) as conn:
        consumed = conn.execute("SELECT replacement_delegation_id FROM async_delegation_child_recovery "
                                "WHERE delegation_id=? AND replacement_delegation_id IS NOT NULL", (did,)).fetchall()
    assert consumed == [], "rejection must neither INSERT a child nor consume a recovery receipt"


@pytest.mark.parametrize("route", ["no_async", "synchronous"])
@pytest.mark.parametrize("shape", ["tasks", "legacy"])
def test_recovery_cannot_bypass_atomic_admission_on_sync_routes(monkeypatch, route, shape):
    did, batch, _ = _legacy_batch(monkeypatch, shape=shape)
    if route == "no_async":
        monkeypatch.setattr(dispatch, "_resolve_async_wake_sid", lambda *_, oneshot_session=None: None)
        result = json.loads(dispatch._dispatch_background(batch))
    else:
        result = json.loads(dispatch._run_batch(batch, background=False))
    assert result["status"] == "rejected"
    assert "durable" in result["error"].lower()
    dispatch._run_sync_with_note.assert_not_called()
    dispatch._execute_and_aggregate.assert_not_called()
    assert dr.retry_status(batch.live_deleg_id) is None
    assert dr.retry_status(did)["resume_attempts"] == 1



def _cut_live_batch(did, *, tui=False):
    checkpointed, release = threading.Event(), threading.Event()

    def runner():
        execution_checkpoints.record_unit_child(did, {"task_index": 0, "status": "completed", "summary": "done"})
        checkpointed.set()
        assert release.wait(5)
        raise RuntimeError("shutdown aggregation failed")

    handle = ad.dispatch_async_delegation_batch(
        goals=["done", "unfinished"], delegation_id=did, context="shared", toolsets=None,
        role="leaf", model="m", session_key="" if tui else "agent:main:telegram:dm:1",
        origin_ui_session_id="tui-owner" if tui else "", parent_session_id=_Parent.session_id,
        runner=runner, interrupt_fn=lambda reason=None: None, max_async_children=1,
    )
    assert handle["accepted"] and checkpointed.wait(5)
    assert ad.interrupt_all(reason="gateway shutdown (restart)") == 1
    release.set()
    event = process_registry.completion_queue.get(timeout=5)
    deadline = time.monotonic() + 5
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ad.active_count() == 0
    claim = ad.claim_event_delivery(event, "test")
    assert claim and ad.complete_completion_delivery(did, claim)
    if not tui:
        dr.schedule_retry(did)
    payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
    assert "tasks" in payload, json.dumps([payload, dr.retry_status(did)], indent=2)
    return payload


def _dispatch_recovery_tasks(tasks, did, monkeypatch, *, tui=False):
    import tools.delegate_tool_dispatch as dispatch
    # Credential/LLM and transcript I/O boundaries only; admission and stop are real.
    tasks = [dict(task, group="recovery") for task in tasks]
    batch = dispatch._Batch(
        task_list=tasks, children=[(i, task, MagicMock()) for i, task in enumerate(tasks)],
        parent_agent=_Parent(), creds={"model": "m"}, context=None, top_role="leaf",
        max_children=len(tasks), live_deleg_id=did, live_writers=[], live_paths=[],
        origin_wake_sid="", origin_ui_session_id="tui-owner" if tui else "",
        origin_owner_transport=None, origin_owner_session_record=None,
        origin_session_history_delivery=False, overall_start=time.monotonic(),
    )
    monkeypatch.setattr(dispatch, "_resolve_async_wake_sid", lambda *args, oneshot_session=None: "")
    monkeypatch.setattr(dispatch, "_resolve_async_session_key", lambda *args: (
        "" if tui else "agent:main:telegram:dm:1", "tui-owner" if tui else ""))
    monkeypatch.setattr(dispatch, "_cleanup_unit", MagicMock())
    executed = MagicMock(return_value={"results": [{"task_index": i, "status": "completed"}
                                                  for i in range(len(tasks))]})
    monkeypatch.setattr(dispatch, "_execute_and_aggregate", executed)
    payload = json.loads(dispatch._run_batch(batch, background=True))
    deadline = time.monotonic() + 5
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ad.active_count() == 0
    if payload["status"] == "dispatched":
        event = process_registry.completion_queue.get(timeout=5)
        assert event.get("delegation_id") == did
        claim = ad.claim_event_delivery(event, "test")
        assert claim and ad.complete_completion_delivery(did, claim)
    return payload, executed


def test_combined_recovery_tasks_from_two_sources_are_rejected_atomically(monkeypatch):
    first, second = "deleg_mixed_source_a", "deleg_mixed_source_b"
    first_payload = _cut_live_batch(first)
    second_payload = _cut_live_batch(second)
    assert first_payload["status"] == second_payload["status"] == "retry_claimed"
    payload, executed = _dispatch_recovery_tasks(
        [*first_payload["tasks"], *second_payload["tasks"]], "deleg_mixed_replacement", monkeypatch,
    )

    assert payload["status"] == "rejected"
    assert "multiple recovery sources" in payload["error"]
    executed.assert_not_called()
    assert dr.retry_status("deleg_mixed_replacement") is None
    with ad._DB_LOCK, ad._transaction() as conn:
        rows = conn.execute(
            "SELECT task_index, replacement_delegation_id FROM async_delegation_child_recovery "
            "WHERE delegation_id IN (?, ?)", (first, second),
        ).fetchall()
    assert rows == [(1, None), (1, None)]
    # Rejected work can be claimed again quietly; no duplicate parent notice.
    assert dr.sweep_retries(now=time.time() + dr.NOTICE_GRACE_S + 1) == []
    for did in (first, second):
        again = json.loads(_handle_control_action("resume", did, None, _Parent()))
        assert again["status"] == "retry_claimed"
        assert [task["goal"] for task in again["tasks"]] == ["unfinished"]


@pytest.mark.parametrize("reason", ["stop_command", "session_end"])
def test_actual_session_stop_revokes_claimed_legacy_recovery(monkeypatch, reason):
    did = "deleg_claimed_stop"
    claimed = _cut_live_batch(did, tui=True)
    assert claimed["status"] == "recovery_claimed"
    affected = ad.interrupt_for_session(origin_ui_session_id="tui-owner", reason=reason)
    payload, executed = _dispatch_recovery_tasks(
        claimed["tasks"], "deleg_claimed_stop_replacement", monkeypatch, tui=True,
    )

    assert payload["status"] == "rejected"
    assert "stopped" in payload["error"]
    executed.assert_not_called()
    assert affected == 1
    assert dr.retry_status("deleg_claimed_stop_replacement") is None
    assert dr.retry_status(did)["retry_state"] == dr.RETRY_CANCELLED
    assert "error" in json.loads(_handle_control_action("resume", did, None, _Parent()))


@pytest.mark.parametrize("route", ["schedule", "sweep"])
def test_retry_scheduler_and_execution_seal_have_one_sqlite_winner(monkeypatch, route):
    from concurrent.futures import ThreadPoolExecutor

    did = _batch_row("deleg_seal_scheduler_race")
    now = time.time()
    with sqlite3.connect(ad._db_path()) as conn:
        conn.execute("UPDATE async_delegations SET retry_state=?, retry_due_at=?, "
                     "delivery_state='delivered' WHERE delegation_id=?",
                     ("armed" if route == "schedule" else "scheduled", now - 60, did))
    checked, release_read = threading.Event(), threading.Event()
    original_gate = dr._recovery_admission_error
    before, receipts_before = None, []

    def gated_gate(record):
        reason = original_gate(record)
        if record["delegation_id"] == did and not checked.is_set():
            checked.set()
            assert release_read.wait(10)
        return reason

    monkeypatch.setattr(dr, "_recovery_admission_error", gated_gate)
    advance = (lambda: dr.schedule_retry(did, now=now)) if route == "schedule" else (
        lambda: dr.sweep_retries(now=now))
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(advance)
        try:
            assert checked.wait(10)
            with sqlite3.connect(ad._db_path(), timeout=0.01) as competitor:
                try:
                    competitor.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    assert "locked" in str(exc)
                    seal_won = False
                else:
                    task = json.loads(competitor.execute(
                        "SELECT task_json FROM async_delegations WHERE delegation_id=?", (did,),
                    ).fetchone()[0])
                    task["execution_sealed_at"] = now
                    competitor.execute("UPDATE async_delegations SET task_json=? WHERE delegation_id=?",
                                       (json.dumps(task), did))
                    before = competitor.execute("SELECT * FROM async_delegations WHERE delegation_id=?",
                                                (did,)).fetchone()
                    receipts_before = competitor.execute(
                        "SELECT * FROM async_delegation_child_recovery WHERE delegation_id=?", (did,),
                    ).fetchall()
                    seal_won = True
        finally:
            release_read.set()
        actions = result.result(timeout=10)
    with sqlite3.connect(ad._db_path()) as conn:
        after = conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?", (did,)).fetchone()
        receipts_after = conn.execute(
            "SELECT * FROM async_delegation_child_recovery WHERE delegation_id=?", (did,),
        ).fetchall()
    if seal_won:
        assert after == before, "a stale scheduler read must not mutate a durably sealed execution"
        assert receipts_after == receipts_before
        assert not actions, "a losing scheduler cannot publish a retry notice or terminal report"
    else:
        assert actions, "the admission winner must preserve ordinary unsealed retry processing"


@pytest.mark.parametrize("route", ["manual", "batch", "retry"])
def test_recovery_claim_and_execution_seal_have_one_sqlite_admission_winner(monkeypatch, route):
    from concurrent.futures import ThreadPoolExecutor

    did = _batch_row("deleg_seal_claim_race")
    with sqlite3.connect(ad._db_path()) as conn:
        if route != "batch":
            task = json.loads(conn.execute(
                "SELECT task_json FROM async_delegations WHERE delegation_id=?", (did,),
            ).fetchone()[0])
            task.update(goal="recover once", goals=["recover once"])
            task.pop("task_indexes", None)
            conn.execute("UPDATE async_delegations SET task_json=?, result_json=? WHERE delegation_id=?",
                         (json.dumps(task), json.dumps({"results": [{"task_index": 0, "status": "interrupted"}]}), did))
        if route == "retry":
            conn.execute("UPDATE async_delegations SET retry_state='scheduled', retry_due_at=? WHERE delegation_id=?",
                         (time.time() - 1, did))
    loaded, release_read = threading.Event(), threading.Event()
    real_load = dr._load

    def gated_load(conn, delegation_id):
        record = real_load(conn, delegation_id)
        if delegation_id == did and not loaded.is_set():
            loaded.set()
            assert release_read.wait(10)
        return record

    monkeypatch.setattr(dr, "_load", gated_load)
    claim = {"manual": dr.claim_resume, "batch": dr.claim_batch_resume, "retry": dr.claim_retry_dispatch}[route]
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(claim, did)
        try:
            assert loaded.wait(10)
            # Independent SQLite writer, not the process-local Python mutex.
            with sqlite3.connect(ad._db_path(), timeout=0.01) as competitor:
                try:
                    competitor.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    assert "locked" in str(exc)
                    seal_won = False
                else:
                    task = json.loads(competitor.execute(
                        "SELECT task_json FROM async_delegations WHERE delegation_id=?", (did,),
                    ).fetchone()[0])
                    task["execution_sealed_at"] = time.time()
                    competitor.execute("UPDATE async_delegations SET task_json=? WHERE delegation_id=?",
                                       (json.dumps(task), did))
                    seal_won = True
        finally:
            release_read.set()
        record, reason = result.result(timeout=10)
    assert record is not None
    if seal_won:
        assert reason == dr.INELIGIBLE_SEALED, "a stale pre-seal read must not admit a post-seal recovery claim"
    else:
        assert reason is None
    with sqlite3.connect(ad._db_path()) as conn:
        state, attempts = conn.execute(
            "SELECT resume_state, resume_attempts FROM async_delegations WHERE delegation_id=?", (did,),
        ).fetchone()
        receipts = conn.execute("SELECT COUNT(*) FROM async_delegation_child_recovery WHERE delegation_id=?",
                                (did,)).fetchone()[0]
    if seal_won:
        assert state == dr.RESUME_STATE_NONE and attempts == 0 and receipts == 0
    elif route == "batch":
        assert state == dr.RESUME_STATE_CLAIMED and attempts == 1 and receipts == 2
    else:
        assert attempts == (1 if route == "manual" else 0)


@pytest.mark.parametrize("shape", ["tasks", "legacy"])
def test_replacement_admission_and_execution_seal_have_one_sqlite_winner(monkeypatch, shape):
    from concurrent.futures import ThreadPoolExecutor
    from typing import cast

    source_before, receipts_before = None, []
    did, batch, _ = _legacy_batch(monkeypatch, shape=shape)
    checked, release_read = threading.Event(), threading.Event()
    original_gate = dr.replacement_admission_error

    def gated_gate(conn, record, task_json):
        reason = original_gate(conn, record, task_json)
        checked.set()
        assert release_read.wait(10)
        return reason

    monkeypatch.setattr(dr, "replacement_admission_error", gated_gate)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(dispatch._dispatch_background, cast(dispatch._Batch, batch))
        try:
            assert checked.wait(10)
            with sqlite3.connect(ad._db_path(), timeout=0.01) as competitor:
                try:
                    competitor.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    assert "locked" in str(exc)
                    seal_won = False
                else:
                    task = json.loads(competitor.execute(
                        "SELECT task_json FROM async_delegations WHERE delegation_id=?", (did,),
                    ).fetchone()[0])
                    task["execution_sealed_at"] = time.time()
                    competitor.execute("UPDATE async_delegations SET task_json=? WHERE delegation_id=?",
                                       (json.dumps(task), did))
                    source_before = competitor.execute(
                        "SELECT * FROM async_delegations WHERE delegation_id=?", (did,),
                    ).fetchone()
                    receipts_before = competitor.execute(
                        "SELECT * FROM async_delegation_child_recovery WHERE delegation_id=?", (did,),
                    ).fetchall()
                    seal_won = True
        finally:
            release_read.set()
        payload = json.loads(result.result(timeout=10))
    with sqlite3.connect(ad._db_path()) as conn:
        replacement = conn.execute("SELECT delegation_id FROM async_delegations WHERE delegation_id=?",
                                   (batch.live_deleg_id,)).fetchone()
        source_after = conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?", (did,)).fetchone()
        receipts_after = conn.execute("SELECT * FROM async_delegation_child_recovery WHERE delegation_id=?",
                                      (did,)).fetchall()
    if seal_won:
        assert payload["status"] == "rejected", "a post-seal insertion must not consume the prior recovery claim"
        assert replacement is None
        assert source_after == source_before and receipts_after == receipts_before
    else:
        assert payload["status"] == "queued"
        assert replacement == (batch.live_deleg_id,)
    cast(Mock, dispatch._run_sync_with_note).assert_not_called()
    cast(Mock, dispatch._execute_and_aggregate).assert_not_called()


@pytest.mark.parametrize("linkage", ["scalar", "retry_root", "child", "mixed"])
@pytest.mark.parametrize("request_from", ["root", "left", "native_child"])
@pytest.mark.parametrize("gap", ["none", "live_child", "missing_record"])
def test_explicit_seal_closes_the_complete_recovery_lineage(linkage, request_from, gap):
    from tools import async_delegation_completion as execution

    ids = {name: f"deleg_closed_lineage_{name}" for name in ("root", "left", "right")}
    now = time.time()
    for name, did in ids.items():
        if name == "right" and gap == "missing_record":
            continue
        _batch_row(did)
        with sqlite3.connect(ad._db_path()) as conn:
            task = json.loads(conn.execute("SELECT task_json FROM async_delegations WHERE delegation_id=?",
                                           (did,)).fetchone()[0])
            task["child_execution"] = {
                str(index): {"child_session_id": f"{did}-child-{index}",
                             "checkpoint": "finished_child", "finished_at": now}
                for index in task["task_indexes"]
            }
            task["unit_execution"] = {"checkpoint": "finished_unit", "finished_at": now}
            if name == "right" and gap == "live_child":
                task["child_execution"]["2"].update(checkpoint="admitted_child", finished_at=None)
            conn.execute("UPDATE async_delegations SET task_json=? WHERE delegation_id=?", (json.dumps(task), did))
    with sqlite3.connect(ad._db_path()) as conn:
        if linkage == "scalar":
            for source, target in (("root", "left"), ("left", "right")):
                conn.execute("UPDATE async_delegations SET retry_replacement=? WHERE delegation_id=?",
                             (ids[target], ids[source]))
        elif linkage == "retry_root":
            # A dangling retry-root reference is also unknown lineage, not a
            # reason to silently discard the missing member from closure.
            for source in ("left", "right"):
                conn.execute("UPDATE async_delegations SET retry_root=? WHERE delegation_id=?",
                             (ids["root"], ids[source]))
            if gap == "missing_record":
                conn.execute("UPDATE async_delegations SET retry_root=? WHERE delegation_id=?",
                             (ids["right"], ids["root"]))
        else:
            edges = (("root", "left", 1), ("root", "right", 2)) if linkage == "child" else (
                ("left", "right", 1),)
            for source, target, index in edges:
                conn.execute("INSERT INTO async_delegation_child_recovery "
                             "(delegation_id, task_index, claim, claimed_at, replacement_delegation_id) "
                             "VALUES (?, ?, ?, ?, ?)", (ids[source], index, "native-closure-claim", now, ids[target]))
            if linkage == "mixed":
                conn.execute("UPDATE async_delegations SET retry_replacement=? WHERE delegation_id=?",
                             (ids["left"], ids["root"]))
                conn.execute("UPDATE async_delegations SET retry_root=? WHERE delegation_id=?",
                             (ids["root"], ids["left"]))

    def rows():
        with sqlite3.connect(ad._db_path()) as conn:
            conn.row_factory = sqlite3.Row
            return ({row["delegation_id"]: dict(row) for row in conn.execute(
                        "SELECT * FROM async_delegations ORDER BY delegation_id")},
                    [dict(row) for row in conn.execute(
                        "SELECT * FROM async_delegation_child_recovery ORDER BY delegation_id, task_index")])

    before, receipts_before = rows()
    requested = ids["left" if request_from == "native_child" else request_from]
    selector = "" if request_from == "native_child" else requested
    assert execution.seal_execution(selector, child_session_id=f"{requested}-child-0") is (gap == "none"), (
        "closure must cover every scalar, shared-root and per-child replacement before returning success"
    )
    after, receipts_after = rows()
    assert receipts_after == receipts_before
    assert after.keys() == before.keys()
    if gap != "none":
        assert after == before, "one unknown member must leave the whole lineage untouched"
        return
    for did in before:
        before_task = json.loads(before[did].pop("task_json"))
        after_task = json.loads(after[did].pop("task_json"))
        assert after[did] == before[did], "closure must not rewrite outcome, delivery or claim state"
        assert after_task.pop("execution_sealed_at") >= now
        assert after_task == before_task
        assert dr.claim_resume(did)[1] == dr.INELIGIBLE_SEALED
    closed, closed_receipts = rows()
    assert execution.seal_execution(selector, child_session_id=f"{requested}-child-0")
    assert rows() == (closed, closed_receipts), "repeated closure must preserve every seal timestamp"


@pytest.mark.parametrize("native_id", ["unknown-native-child", "ambiguous-native-child"])
def test_native_only_seal_requires_a_unique_durable_identity(native_id):
    from tools import async_delegation_completion as execution

    now = time.time()
    for did in ("deleg_native_identity_a", "deleg_native_identity_b"):
        _batch_row(did)
        with sqlite3.connect(ad._db_path()) as conn:
            task = json.loads(conn.execute("SELECT task_json FROM async_delegations WHERE delegation_id=?",
                                           (did,)).fetchone()[0])
            task["child_execution"] = {
                str(i): {"child_session_id": "ambiguous-native-child" if i == 0 else f"{did}-child-{i}",
                         "checkpoint": "finished_child", "finished_at": now}
                for i in task["task_indexes"]
            }
            task["unit_execution"] = {"checkpoint": "finished_unit", "finished_at": now}
            conn.execute("UPDATE async_delegations SET task_json=? WHERE delegation_id=?", (json.dumps(task), did))
    with sqlite3.connect(ad._db_path()) as conn:
        before = conn.execute("SELECT * FROM async_delegations ORDER BY delegation_id").fetchall()
    assert not execution.seal_execution("", child_session_id=native_id)
    with sqlite3.connect(ad._db_path()) as conn:
        assert conn.execute("SELECT * FROM async_delegations ORDER BY delegation_id").fetchall() == before
        assert conn.execute("SELECT * FROM async_delegation_child_recovery").fetchall() == []


@pytest.mark.parametrize("gap", [
    "none", "missing_child", "admitted_child", "missing_unit", "legacy",
    "wrong_native_child", "unknown_unit", "linked_replacement",
])
@pytest.mark.parametrize("by_native_child", [False, True])
def test_explicit_execution_seal_requires_every_positive_checkpoint(gap, by_native_child):
    from tools import async_delegation_completion as execution

    did = _batch_row("deleg_execution_seal_policy")
    sid = f"{did}-child-0"
    with ad._DB_LOCK, ad._transaction() as conn:
        task = json.loads(conn.execute(
            "SELECT task_json FROM async_delegations WHERE delegation_id=?", (did,),
        ).fetchone()[0])
        now = time.time()
        task["child_execution"] = {
            str(i): {"child_session_id": f"{did}-child-{i}", "checkpoint": "finished_child",
                     "admitted_at": now - 1, "finished_at": now}
            for i in range(3)
        }
        task["unit_execution"] = {"checkpoint": "finished_unit", "finished_at": now}
        mutations = {
            "missing_child": lambda: task["child_execution"].pop("2"),
            "admitted_child": lambda: task["child_execution"].update({
                "2": {"child_session_id": f"{did}-child-2", "checkpoint": "admitted", "admitted_at": now - 1},
            }),
            "missing_unit": lambda: task.pop("unit_execution"),
            "legacy": lambda: task.pop("child_execution"),
            "unknown_unit": lambda: task.update(unit_execution={"checkpoint": "unknown", "finished_at": now}),
        }
        mutations.get(gap, lambda: None)()
        sid = {"wrong_native_child": "unrelated-native-child"}.get(gap, sid)
        conn.execute("UPDATE async_delegations SET task_json=?, retry_state='reported' WHERE delegation_id=?",
                     (json.dumps(task), did))
        if gap == "linked_replacement":
            conn.execute("UPDATE async_delegations SET retry_replacement='deleg_missing_replacement' "
                         "WHERE delegation_id=?", (did,))
    with sqlite3.connect(ad._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        before = dict(conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?", (did,)).fetchone())
    assert execution.seal_execution("" if by_native_child else did, child_session_id=sid) is (gap == "none")
    with sqlite3.connect(ad._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        after = dict(conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?", (did,)).fetchone())
    if gap != "none":
        assert after == before, "refused closure must preserve the complete outcome, delivery and claim row"
        return
    before_task, after_task = json.loads(before.pop("task_json")), json.loads(after.pop("task_json"))
    assert after == before
    assert after_task.pop("execution_sealed_at") >= now
    assert after_task == before_task
    # Reported/terminal states were never used as execution evidence. Once the
    # positive checkpoints close admission, the existing claim gate must agree.
    assert dr.claim_resume(did)[1] == dr.INELIGIBLE_SEALED
    assert "error" in json.loads(_handle_control_action("resume", did, None, _Parent()))
