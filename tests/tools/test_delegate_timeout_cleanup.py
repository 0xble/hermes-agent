"""Regression coverage for timed-out delegation teardown."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from tools import delegate_tool


class _SlowUnwindingChild:
    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        self.tool_progress_callback = None
        self._credential_pool = None
        self._delegate_saved_tool_names = []
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._subagent_id = None
        self.model = "test-model"
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self.started = threading.Event()
        self.interrupted = threading.Event()
        self.unwinding = threading.Event()
        self.allow_finish = threading.Event()
        self.finished = threading.Event()
        self.closed = threading.Event()
        self.close_while_running = False

    def run_conversation(self, **_kwargs):
        self.started.set()
        # Generous bounds: these gate on events the test sets promptly; a tight bound only
        # turns a load-starved test thread into a spurious early exit that fires close().
        assert self.interrupted.wait(timeout=30)
        # Model the real child turn's finally path: it still performs session
        # activity/SQLite cleanup after the parent requests interruption.
        self.unwinding.set()
        assert self.allow_finish.wait(timeout=30)
        self.finished.set()
        return {
            "final_response": "",
            "completed": False,
            "interrupted": True,
            "api_calls": 1,
            "messages": [],
        }

    def hard_interrupt(self, _reason=None):
        self.interrupted.set()

    def get_activity_summary(self):
        return {"api_call_count": 1}

    def close(self):
        if not self.finished.is_set():
            self.close_while_running = True
        self.closed.set()


def test_timeout_does_not_close_child_while_worker_is_unwinding(monkeypatch):
    child = _SlowUnwindingChild()
    parent = SimpleNamespace(
        session_id="parent-timeout-test",
        _current_task_id=None,
        _active_children=[child],
        _active_children_lock=threading.Lock(),
    )
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.5)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)

    from tools.daemon_pool import DaemonThreadPoolExecutor

    submit = DaemonThreadPoolExecutor.submit

    def submit_started(executor, *args, **kwargs):
        future = submit(executor, *args, **kwargs)
        assert child.started.wait(timeout=10)
        return future

    # Timeout accounting starts only after this test's child is running.
    monkeypatch.setattr(DaemonThreadPoolExecutor, "submit", submit_started)
    result = delegate_tool._run_single_child(
        task_index=0,
        goal="exercise timeout teardown",
        child=child,
        parent_agent=parent,
    )

    assert result["status"] == "timeout"
    assert child.unwinding.wait(timeout=10)
    try:
        assert not child.closed.is_set(), (
            "timed-out child.close() ran before its conversation thread unwound"
        )
    finally:
        child.allow_finish.set()
    assert child.finished.wait(timeout=10)
    assert child.closed.wait(timeout=10)
    assert not child.close_while_running, (
        "timed-out child.close() raced its still-running conversation thread"
    )


def test_finished_child_checkpoint_arrives_after_timeout_without_rewriting_outcome(monkeypatch, tmp_path):
    import json
    import sqlite3
    import time
    from contextlib import closing
    from tools import async_delegation as ad, delegate_tool_child_run as child_run
    from tools.delegate_tool_dispatch import _Batch, _dispatch_unit
    from tools.process_registry import process_registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    did = "deleg_late_finished_child"
    child = _SlowUnwindingChild("child-late-finished-checkpoint")
    parent = SimpleNamespace(session_id="parent-late-finished", _current_task_id=None)
    deferred = []
    callbacks_done = threading.Event()
    calls = []
    converse = child.run_conversation
    defer_close = child_run._defer_close_after_timeout
    wait_liveness = child_run._ChildRun.wait_liveness_aware

    def counted_converse(**kwargs):
        calls.append(kwargs["user_message"])
        return converse(**kwargs)

    def observe_defer(native_child, future):
        deferred.append(future)
        return defer_close(native_child, future)

    def wait_after_start(run, settled, future, timeout):
        assert child.started.wait(10)
        return wait_liveness(run, settled, future, timeout)

    monkeypatch.setattr(child, "run_conversation", counted_converse)
    monkeypatch.setattr(child_run, "_defer_close_after_timeout", observe_defer)
    monkeypatch.setattr(child_run._ChildRun, "wait_liveness_aware", wait_after_start)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.5)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    task = {"goal": "finish only after the timeout was reported"}
    unit = _Batch(
        task_list=[task], children=[(0, task, child)], parent_agent=parent,
        creds={"model": "test-model"}, context=None, top_role="leaf", max_children=1,
        live_deleg_id=None, live_writers=[], live_paths=[], origin_wake_sid="",
        origin_ui_session_id="", origin_owner_transport=None,
        origin_owner_session_record=None, origin_session_history_delivery=False,
        overall_start=time.monotonic(), unit_id=did,
    )

    def outcome_row():
        with closing(sqlite3.connect(ad._db_path())) as conn:
            return conn.execute("""SELECT state, result_json, completed_at, updated_at,
                delivery_state, delivery_attempts, retry_state
                FROM async_delegations WHERE delegation_id=?""", (did,)).fetchone()

    try:
        handle = _dispatch_unit(unit, did, None, {
            "session_key": "session", "parent_session_id": parent.session_id,
            "max_async_children": 1,
        })
        assert handle["accepted"]
        event = process_registry.completion_queue.get(timeout=10)
        assert event["results"][0]["status"] == "timeout"
        assert child.unwinding.wait(10)
        assert not child.closed.is_set()
        durable = ad.get_durable_delegation(did)
        assert durable is not None
        assert durable["child_execution"]["0"]["checkpoint"] == "admitted"
        before = outcome_row()
        assert before is not None
        assert json.loads(before[1])["results"][0]["status"] == "timeout"
        assert len(deferred) == 1
        # This tail runs after callbacks registered by both the abandoned
        # conversation and its already-returned driver. No polling or fake ACK.
        deferred[0].add_done_callback(lambda _future: callbacks_done.set())
        child.allow_finish.set()
        assert callbacks_done.wait(10)
        assert child.finished.is_set() and child.closed.is_set()
        durable = ad.get_durable_delegation(did)
        assert durable is not None
        checkpoint = durable["child_execution"]["0"]
        assert checkpoint["checkpoint"] == "finished_child", (
            "a late finished-child checkpoint must survive an already-terminal timeout"
        )
        assert checkpoint["child_session_id"] == child.session_id
        assert outcome_row() == before, "a late ACK must not rewrite the result or delivery state"
        assert calls == [task["goal"]]
        assert process_registry.completion_queue.empty()
    finally:
        child.allow_finish.set()
        if deferred:
            deferred[0].result(timeout=10)
        ad._reset_for_tests()
