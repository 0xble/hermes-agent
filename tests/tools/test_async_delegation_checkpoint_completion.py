"""Checkpoint truth survives terminal aggregation and shutdown failures."""

import json
import queue
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    yield
    deadline = time.monotonic() + 5
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()


@pytest.mark.parametrize("shape", ["one_goal", "scalar", "grouped", "mixed"])
@pytest.mark.parametrize("cut", [True, False])
def test_shutdown_runner_crash_preserves_completed_checkpoint(shape, cut):
    checkpointed = threading.Event()
    release = threading.Event()
    did = "deleg_checkpoint_crash"
    indexes = [9, 4] if shape in {"grouped", "mixed"} else [0]
    children = [{"task_index": i, "status": "completed", "summary": f"durable success {i}"}
                for i in (indexes[:1] if shape == "mixed" else indexes)]

    def runner():
        for child in children:
            ad.record_unit_child(did, child)
        checkpointed.set()
        assert release.wait(5)
        raise RuntimeError("shutdown aggregation failed")

    kwargs: dict[str, Any] = dict(delegation_id=did, context="ctx", toolsets=None,
                  role="leaf", model="m", session_key="session", parent_session_id="parent",
                  runner=runner, interrupt_fn=lambda reason=None: release.set(), max_async_children=1)
    if shape == "scalar":
        handle = ad.dispatch_async_delegation(goal="one", **kwargs)
    else:
        grouped = shape in {"grouped", "mixed"}
        handle = ad.dispatch_async_delegation_batch(
            goals=["one"] if not grouped else [f"goal-{i}" for i in range(10)],
            task_indexes=indexes if grouped else None, **kwargs)
    assert handle["accepted"]
    assert checkpointed.wait(5)
    if cut:
        assert ad.interrupt_all("gateway shutdown (post-interrupt)") == 1
    else:
        release.set()
    event = process_registry.completion_queue.get(timeout=5)
    with ad._DB_LOCK, ad._transaction() as conn:
        state, raw = conn.execute(
            "SELECT state, result_json FROM async_delegations WHERE delegation_id=?", (did,)
        ).fetchone()
    result = json.loads(raw)
    if shape == "scalar":
        assert result["summary"] == children[0]["summary"]
        assert event["summary"] == children[0]["summary"]
    else:
        assert [child["task_index"] for child in result["results"]] == indexes
        assert result["results"] == event["results"]
        assert result["results"][:len(children)] == children
    # Mixed units keep the historical display status; the restart cut is durable state only.
    expected = ("interrupted" if cut else "error") if shape == "mixed" else "completed"
    assert state == expected
    assert event["status"] == ("error" if shape == "mixed" else "completed")
    assert ("cut_reason" in event) == (shape == "mixed" and cut)
    from tools.delegation_resume import inspect_resumable
    if shape != "mixed":
        assert inspect_resumable(did)[1] == "not_interrupted"


def _running_record(indexes):
    record = {
        "delegation_id": "deleg_checkpoint_boundary", "status": "running",
        "_durable_state": "running", "dispatched_at": time.time(),
        "completed_at": time.time(), "session_key": "session", "parent_session_id": "parent",
        "is_batch": True, "goals": [f"task-{i}" for i in range(14)],
        "task_indexes": indexes, "task_transcripts": {"9": "/checkpoint/child-9.log"},
        "_cut_reason": "gateway shutdown (post-interrupt)",
    }
    ad._persist_dispatch(record)
    assert ad._persist_transition(record["delegation_id"], "queued", "admitted")
    assert ad._persist_transition(record["delegation_id"], "admitted", "running")
    return record


def _install_terminal_path(monkeypatch, path, late_checkpoint):
    primary, fallback, transaction = ad._persist_completion, ad._persist_outbox_event, ad._transaction
    lock_checks = []

    def assert_writer_fenced(sql):
        if not sql.startswith("SELECT task_json, result_json"):
            return
        # Independent SQLite writer: _DB_LOCK is process-local and is not sufficient.
        with closing(sqlite3.connect(ad._db_path(), timeout=0.01)) as competitor:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("UPDATE async_delegations SET updated_at=updated_at")
        lock_checks.append(True)

    @contextmanager
    def traced_transaction():
        with transaction() as conn:
            conn.set_trace_callback(assert_writer_fenced)
            yield conn

    def primary_write(event, result, **kwargs):
        if path in {"fallback", "ambiguous_fallback", "deferred", "deferred_finalizing"}:
            raise RuntimeError("initial persistence unavailable")
        late_checkpoint()
        wrote = primary(event, result, **kwargs)
        if path == "ambiguous_primary":
            raise RuntimeError("primary committed before error")
        return wrote

    def fallback_write(event, result, **kwargs):
        late_checkpoint()
        wrote = fallback(event, result, **kwargs)
        if path == "ambiguous_fallback":
            raise RuntimeError("fallback committed before error")
        return wrote

    monkeypatch.setattr(ad, "_transaction", traced_transaction)
    monkeypatch.setattr(ad, "_persist_completion", primary_write)
    monkeypatch.setattr(ad, "_persist_outbox_event", fallback_write)
    return lock_checks


@pytest.mark.parametrize("path", ["primary", "fallback", "ambiguous_primary", "ambiguous_fallback", "deferred", "deferred_finalizing", "budgeted_primary"])
@pytest.mark.parametrize("complete", [True, False])
def test_terminal_ownership_reconciles_late_checkpoints(monkeypatch, path, complete):
    indexes = [9, 4] if complete else [9, 4, 13]
    record = _running_record(indexes)
    did = record["delegation_id"]
    first = {"task_index": 4, "status": "completed", "summary": "first checkpoint"}
    late = {"task_index": 9, "status": "success", "summary": "late checkpoint"}
    if path == "budgeted_primary":
        from tools import delegate_tool
        monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"max_summary_chars": 64})
        late["summary"] *= 1000
    ad.record_unit_child(did, first)
    locks = _install_terminal_path(monkeypatch, path, lambda: ad.record_unit_child(did, late))
    real_reconcile = ad._reconcile_terminal_write
    if path.startswith("deferred"):
        record["status"] = "finalizing" if path == "deferred_finalizing" else "error"
        with ad._records_lock:
            ad._records[did] = record
        monkeypatch.setattr(ad, "_reconcile_terminal_write", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("read unavailable")))
    # Even explicit interrupted/unknown aggregate entries cannot downgrade checkpoint success.
    result = {"results": [{"task_index": 4, "status": "interrupted"}], "error": "aggregation failed"}
    ad._push_completion_event(record, result, "error")
    if path.startswith("deferred"):
        pending = process_registry.completion_queue.get(timeout=5)
        assert ad.claim_event_delivery(pending, "parent") is None
        monkeypatch.setattr(ad, "_reconcile_terminal_write", real_reconcile)
        assert ad.reoffer_unresolved_completions(process_registry.completion_queue, now=time.time() + 1000) == 1
    event = process_registry.completion_queue.get(timeout=5)
    claim = ad.claim_event_delivery(event, "parent")
    assert claim
    expected = "completed" if complete else "interrupted"
    display = "completed" if complete else "error"
    assert event["status"] == display
    assert [child["task_index"] for child in event["results"]] == indexes
    by_index = {child["task_index"]: child for child in event["results"]}
    assert by_index[4] == first
    if path == "budgeted_primary":
        assert by_index[9]["summary_truncated"]
        assert len(by_index[9]["summary"]) < len(late["summary"])
        assert Path(by_index[9]["summary_full_path"]).read_text() == late["summary"]
    else:
        assert by_index[9]["summary"] == late["summary"]
    assert by_index[9]["status"] == "success"
    assert by_index[9]["live_transcript"] == record["task_transcripts"]["9"]
    if not complete:
        assert by_index[13]["status"] == "unknown"
        assert event["cut_reason"] == record["_cut_reason"]
    else:
        assert "cut_reason" not in event
    with ad._DB_LOCK, ad._transaction() as conn:
        state, raw = conn.execute("SELECT state, result_json FROM async_delegations WHERE delegation_id=?", (did,)).fetchone()
    assert state == expected
    assert json.loads(raw)["results"] == event["results"]
    if path == "deferred_finalizing":
        # The resolver can win before the producer's original finalization tail; run only that tail.
        monkeypatch.setattr(ad, "_push_completion_event", lambda *a, **kw: None)
        ad._finalize(did, result, "error", _claimed_snapshot=dict(record, _durable_state="running"))
    if path.startswith("deferred"):
        assert (record["status"], record["_durable_state"]) == (display, expected)
    assert locks, "checkpoint read and terminal claim must exclude independent SQLite writers"
    ad.complete_event_delivery(event, claim)
    assert ad.claim_event_delivery(event, "other-parent") is None
    assert ad.restore_undelivered_completions(queue.Queue()) == 0
    assert process_registry.completion_queue.empty()
