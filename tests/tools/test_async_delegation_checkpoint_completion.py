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
from tools import async_delegation_completion as execution_checkpoints
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
            execution_checkpoints.record_unit_child(did, child)
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
    execution_checkpoints.record_unit_child(did, first)
    locks = _install_terminal_path(monkeypatch, path, lambda: execution_checkpoints.record_unit_child(did, late))
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


@pytest.mark.parametrize("schema_retry", [False, True])
def test_finished_child_checkpoint_survives_aggregate_crash(monkeypatch, schema_retry):
    from types import SimpleNamespace
    from tools import delegate_tool, delegate_tool_dispatch as dispatch

    did = "deleg_finished_child_checkpoint"
    sid = "child-finished-before-aggregate-crash"
    calls = []
    closed = threading.Event()
    aggregating = threading.Event()
    unit_finished = threading.Event()
    retry_started = threading.Event()
    release_retry = threading.Event()
    summary = '{"value":"durable child success"}' if schema_retry else "durable child success"

    def converse(**kwargs):
        calls.append(kwargs["user_message"])
        text = summary
        if schema_retry:
            if len(calls) == 1:
                text = "not valid JSON"
            else:
                retry_started.set()
                assert release_retry.wait(10)
        return {"final_response": text, "completed": True, "api_calls": 1, "messages": []}

    child = SimpleNamespace(
        session_id=sid, model="test-model", tool_progress_callback=None,
        _credential_pool=None, _delegate_saved_tool_names=[], _delegate_role="leaf",
        _delegate_depth=1, _subagent_id=None, _delegation_id=did,
        _delegate_output_schema=({"type": "object", "properties": {"value": {"type": "string"}},
                                  "required": ["value"]} if schema_retry else None),
        run_conversation=converse, close=closed.set,
        get_activity_summary=lambda: {"api_call_count": len(calls)},
    )
    task = {"goal": "finish once before aggregation fails"}
    parent = SimpleNamespace(session_id="parent-finished-child-checkpoint")
    unit = dispatch._Batch(
        task_list=[task], children=[(0, task, child)], parent_agent=parent,
        creds={"model": "test-model"}, context=None, top_role="leaf", max_children=1,
        live_deleg_id=None, live_writers=[], live_paths=[], origin_wake_sid="",
        origin_ui_session_id="", origin_owner_transport=None,
        origin_owner_session_record=None, origin_session_history_delivery=False,
        overall_start=time.monotonic(), unit_id=did,
    )

    def crash_after_child(*_args, **_kwargs):
        assert closed.is_set(), "aggregate failure must follow the real child teardown"
        aggregating.set()
        raise RuntimeError("aggregate failed after finished child")

    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)
    monkeypatch.setattr(dispatch, "_finalize_child_results", crash_after_child)
    handle = dispatch._dispatch_unit(unit, did, None, {
        "session_key": "session", "parent_session_id": parent.session_id,
        "max_async_children": 1,
    })
    assert handle["accepted"]
    with ad._records_lock:
        future = ad._records[did]["_future"]
    future.add_done_callback(lambda _future: unit_finished.set())
    try:
        if schema_retry:
            assert retry_started.wait(10)
            pending = ad.get_durable_delegation(did)
            assert pending is not None
            assert pending["child_execution"]["0"]["checkpoint"] == "admitted", (
                "the first conversation Future cannot finish a still-active schema-retry driver"
            )
            assert not closed.is_set()
    finally:
        release_retry.set()
    assert aggregating.wait(10)
    assert unit_finished.wait(10)
    event = process_registry.completion_queue.get(timeout=10)
    assert event["status"] == "completed"
    assert event["results"][0]["summary"] == summary
    assert calls[0] == task["goal"]
    assert len(calls) == (2 if schema_retry else 1), "the bounded schema retry must never replay completed work"
    if schema_retry:
        assert event["results"][0]["schema_valid"] is True
        assert event["results"][0]["schema_retries"] == 1

    durable = ad.get_durable_delegation(did)
    assert durable is not None
    assert durable["unit_execution"]["checkpoint"] == "finished_unit", (
        "a crashed aggregate driver must close without replaying its completed child"
    )
    checkpoint = durable.get("child_execution", {}).get("0", {})
    assert checkpoint.get("checkpoint") == "finished_child", (
        "finished exact-child checkpoint was lost when aggregation failed"
    )
    assert checkpoint["child_session_id"] == sid
    assert checkpoint["finished_at"] >= checkpoint["admitted_at"]
    with closing(sqlite3.connect(ad._db_path())) as conn:
        raw = conn.execute("SELECT task_json FROM async_delegations WHERE delegation_id=?", (did,)).fetchone()[0]
    assert json.loads(raw)["child_execution"]["0"] == checkpoint


def test_failed_enqueue_cannot_enter_after_a_new_submission_owns_the_unit(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    occupied = threading.Event()
    release_occupier = threading.Event()
    failed_submit = threading.Event()
    gates = [threading.Event(), threading.Event()]
    effects = []
    did = "deleg_obsolete_submission"
    monkeypatch.setattr(ad, "_ensure_stale_monitor", lambda: None)
    first = ad.dispatch_async_delegation(
        goal="occupy the slot", context=None, toolsets=None, role="leaf", model="m", session_key="session",
        runner=lambda: (occupied.set(), release_occupier.wait(10), {"status": "completed"})[2],
        max_async_children=1,
    )
    assert occupied.wait(10)
    queued = ad.dispatch_async_delegation_batch(
        delegation_id=did, goals=["only the accepted submission may run"],
        context=None, toolsets=None, role="leaf", model="m", session_key="session",
        parent_session_id="parent-obsolete-submission", child_sessions={"0": "child-current-submission"},
        runner=lambda: (effects.append("ran"), {"results": []})[1], max_async_children=1,
        max_queued_delegations=1,
    )
    assert queued["status"] == "queued", queued
    futures = []

    with ThreadPoolExecutor(max_workers=1) as executor:
        class EnqueueThenFailOnceExecutor:
            def submit(self, fn, arg):
                index = len(futures)

                def delayed_entry():
                    assert gates[index].wait(10)
                    return fn(arg)

                future = executor.submit(delayed_entry)
                futures.append(future)
                if index == 0:
                    failed_submit.set()
                    raise RuntimeError("enqueued before submit failed")
                return future

        monkeypatch.setattr(ad, "_get_executor", lambda _capacity: EnqueueThenFailOnceExecutor())
        try:
            release_occupier.set()
            assert failed_submit.wait(10)
            with ad._records_lock:
                assert ad._records[did]["status"] == "queued"
                assert ad._records[did]["_future"] is None
            ad._admit_pending()
            with ad._records_lock:
                current_future = ad._records[did]["_future"]
                assert ad._records[did]["status"] == "running"
            assert len(futures) == 2 and current_future is futures[1]
            gates[0].set()
            futures[0].result(timeout=10)
            assert effects == [], "an obsolete enqueued callable must not steal a later submission's admission"
            pending = ad.get_durable_delegation(did)
            assert pending is not None and pending["state"] == "running"
            assert pending["unit_execution"] == {}
            gates[1].set()
            current_future.result(timeout=10)
            assert effects == ["ran"]
            events = [process_registry.completion_queue.get(timeout=10) for _ in range(2)]
            assert {event["delegation_id"] for event in events} == {first["delegation_id"], did}
            assert process_registry.completion_queue.empty()
        finally:
            release_occupier.set()
            for gate in gates:
                gate.set()


def test_terminalized_unit_does_not_enter_runner_when_executor_later_starts(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    did = "deleg_terminal_before_driver_start"
    occupied = threading.Event()
    release = threading.Event()
    callbacks_done = threading.Event()
    effects = []

    def occupy_executor():
        occupied.set()
        assert release.wait(10)

    def stored_row():
        with closing(sqlite3.connect(ad._db_path())) as conn:
            return conn.execute("""SELECT state, result_json, completed_at, updated_at,
                delivery_state, delivery_attempts, retry_state, task_json
                FROM async_delegations WHERE delegation_id=?""", (did,)).fetchone()

    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(occupy_executor)
        assert occupied.wait(10)
        monkeypatch.setattr(ad, "_get_executor", lambda _capacity: executor)
        try:
            handle = ad.dispatch_async_delegation_batch(
                delegation_id=did, goals=["never start after terminalization"],
                context=None, toolsets=None, role="leaf", model="m", session_key="session",
                parent_session_id="parent-terminal-before-start", child_sessions={"0": "child-never-started"},
                runner=lambda: (effects.append("ran"), {"results": []})[1], max_async_children=1,
            )
            assert handle["accepted"]
            with ad._records_lock:
                future = ad._records[did]["_future"]
                assert not ad._records[did]["_started"]
            future.add_done_callback(lambda _future: callbacks_done.set())
            ad._finalize(did, {"error": "terminalized before executor entry"}, "stalled")
            event = process_registry.completion_queue.get(timeout=10)
            assert event["status"] == "stalled"
            before = stored_row()
            release.set()
            blocker.result(timeout=10)
            future.result(timeout=10)
            assert callbacks_done.wait(10)
            assert effects == [], "a terminal outcome before driver entry must fence later execution"
            assert stored_row() == before, "the skipped driver must not rewrite its terminal outcome"
            durable = ad.get_durable_delegation(did)
            assert durable is not None and durable["unit_execution"] == {}
            assert durable["child_execution"]["0"]["checkpoint"] == "admitted"
            assert process_registry.completion_queue.empty()
        finally:
            release.set()


@pytest.mark.parametrize("prospective", [False, True])
def test_finished_child_checkpoint_stays_unknown_after_owner_loss_without_proof(monkeypatch, prospective):
    did = "deleg_owner_lost_without_finished_proof"
    sid = "child-admitted-before-owner-loss"
    record = {"delegation_id": did, "session_key": "session", "parent_session_id": "parent",
              "dispatched_at": time.time(), "goal": "owner loss is not a finished child",
              "goals": ["owner loss is not a finished child"], "is_batch": True}
    if prospective:
        record["child_sessions"] = {"0": sid}
    ad._persist_dispatch(record)
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET state='running', retry_state='reported' WHERE delegation_id=?", (did,))
    monkeypatch.setattr(ad, "_owner_liveness", lambda: lambda _pid, _started: False)
    assert ad.recover_abandoned_delegations() == 1
    durable = ad.get_durable_delegation(did)
    assert durable is not None
    assert durable["state"] == "unknown"
    assert durable["completed_at"] is not None
    assert durable["unit_execution"] == {}, "owner loss or a terminal row cannot finish the unit driver"
    if prospective:
        assert durable["child_execution"]["0"]["checkpoint"] == "admitted"
        assert "finished_at" not in durable["child_execution"]["0"]
    else:
        assert durable["child_execution"] == {}
        assert not execution_checkpoints.record_child_finished(did, 0, sid), "legacy identity cannot be backfilled by an ACK"
    assert not execution_checkpoints.record_child_finished(did, 0, "different-child-session")
    assert not execution_checkpoints.record_child_finished(did, 1, sid)
    assert ad.get_durable_delegation(did) == durable


@pytest.mark.parametrize("child_only_fresh_process", [False, True])
def test_unit_execution_checkpoint_waits_for_real_stop_hook_completion(monkeypatch, child_only_fresh_process):
    import subprocess
    import sys
    from types import SimpleNamespace
    from hermes_cli import plugins
    from tools import async_delegation_completion as execution
    from tools import delegate_tool, delegate_tool_dispatch as dispatch

    did = "deleg_unit_checkpoint_after_hook"
    sid = "child-finished-before-stop-hook"

    def request_seal():
        if not child_only_fresh_process:
            return execution.seal_execution(did, child_session_id=sid)
        # The consumer's dispatch marker can know only the native child id.
        # A fresh interpreter has no owner roster, Future or plugin state.
        result = subprocess.run(
            [sys.executable, "-c", "import sys; from tools.async_delegation_completion import seal_execution; "
             "print(seal_execution('', child_session_id=sys.argv[1]))", sid],
            check=True, capture_output=True, text=True, timeout=10,
        )
        assert result.stdout.strip() in {"True", "False"}
        return result.stdout.strip() == "True"

    calls = []
    closed = threading.Event()
    hook_entered = threading.Event()
    release_hook = threading.Event()
    hook_finished = threading.Event()
    unit_finished = threading.Event()

    def converse(**kwargs):
        calls.append(kwargs["user_message"])
        return {"final_response": "child finished once", "completed": True,
                "api_calls": 1, "messages": []}

    def invoke_hook(name, **kwargs):
        if name == "subagent_stop":
            assert kwargs["child_session_id"] == sid
            hook_entered.set()
            if release_hook.wait(10):
                hook_finished.set()

    child = SimpleNamespace(
        session_id=sid, model="test-model", tool_progress_callback=None,
        _credential_pool=None, _delegate_saved_tool_names=[], _delegate_role="leaf",
        _delegate_depth=1, _subagent_id=None,
        run_conversation=converse, close=closed.set,
        get_activity_summary=lambda: {"api_call_count": len(calls)},
    )
    task = {"goal": "wait for the completion unit, not only its finished child"}
    parent = SimpleNamespace(session_id="parent-unit-execution-checkpoint")
    unit = dispatch._Batch(
        task_list=[task], children=[(0, task, child)], parent_agent=parent,
        creds={"model": "test-model"}, context=None, top_role="leaf", max_children=1,
        live_deleg_id=None, live_writers=[], live_paths=[], origin_wake_sid="",
        origin_ui_session_id="", origin_owner_transport=None,
        origin_owner_session_record=None, origin_session_history_delivery=False,
        overall_start=time.monotonic(), unit_id=did,
    )
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)
    monkeypatch.setattr(plugins, "invoke_hook", invoke_hook)
    handle = dispatch._dispatch_unit(unit, did, None, {
        "session_key": "session", "parent_session_id": parent.session_id,
        "max_async_children": 1,
    })
    try:
        assert handle["accepted"]
        assert hook_entered.wait(10)
        with ad._records_lock:
            future = ad._records[did]["_future"]
        future.add_done_callback(lambda _future: unit_finished.set())
        assert not future.done()
        pending = ad.get_durable_delegation(did)
        assert pending is not None and closed.is_set()
        assert pending["child_execution"]["0"]["checkpoint"] == "finished_child"
        assert pending.get("unit_execution", {}).get("checkpoint") != "finished_unit"
        assert not request_seal()
        assert ad.get_durable_delegation(did) == pending
    finally:
        release_hook.set()
    assert unit_finished.wait(10)
    assert hook_finished.is_set()
    assert calls == [task["goal"]]
    event = process_registry.completion_queue.get(timeout=10)
    assert event["status"] == "completed"
    assert process_registry.completion_queue.empty()
    durable = ad.get_durable_delegation(did)
    assert durable is not None
    checkpoint = durable.get("unit_execution", {})
    assert checkpoint.get("checkpoint") == "finished_unit", (
        "the actual completion-unit driver must checkpoint after its stop hook finishes"
    )
    assert checkpoint["finished_at"] >= durable["child_execution"]["0"]["finished_at"]
    with closing(sqlite3.connect(ad._db_path())) as conn:
        raw = conn.execute("SELECT task_json FROM async_delegations WHERE delegation_id=?", (did,)).fetchone()[0]
    assert json.loads(raw)["unit_execution"] == checkpoint

    # Closing future admission is an explicit operation, not a side effect of
    # finishing a driver or querying it. It leaves outcome and delivery intact.
    before = durable
    with closing(sqlite3.connect(ad._db_path())) as conn:
        conn.row_factory = sqlite3.Row
        row_before = dict(conn.execute(
            "SELECT * FROM async_delegations WHERE delegation_id=?", (did,),
        ).fetchone())
    assert "execution_sealed_at" not in json.loads(row_before["task_json"])
    assert request_seal()
    with closing(sqlite3.connect(ad._db_path())) as conn:
        conn.row_factory = sqlite3.Row
        row_after = dict(conn.execute(
            "SELECT * FROM async_delegations WHERE delegation_id=?", (did,),
        ).fetchone())
    task_after = json.loads(row_after.pop("task_json"))
    task_before = json.loads(row_before.pop("task_json"))
    assert row_after == row_before
    sealed_at = task_after.pop("execution_sealed_at")
    assert sealed_at >= checkpoint["finished_at"]
    assert task_after == task_before
    assert ad.get_durable_delegation(did) == before
    assert request_seal()
    with closing(sqlite3.connect(ad._db_path())) as conn:
        sealed_task = json.loads(conn.execute(
            "SELECT task_json FROM async_delegations WHERE delegation_id=?", (did,),
        ).fetchone()[0])
    assert sealed_task["execution_sealed_at"] == sealed_at
    assert calls == [task["goal"]]
    assert process_registry.completion_queue.empty()
