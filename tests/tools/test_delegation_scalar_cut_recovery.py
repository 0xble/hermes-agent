"""A cut notice's resume promise must agree with durable scalar admission (PR #561)."""

import json
import queue
import threading
import time

import pytest

from tools import async_delegation as ad
from tools import delegation_resume as dr
from tools.delegate_tool_registry import _handle_control_action
from tools.process_registry import process_registry
from tools.process_registry_notifications import _format_async_delegation


class Parent:
    session_id = "owner"
    _session_db = None


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _await_completion(delegation_id, timeout=5.0):
    """The shared queue may carry other events; wait for this unit's own completion."""
    deadline, others = time.monotonic() + timeout, []
    try:
        while time.monotonic() < deadline:
            try:
                candidate = process_registry.completion_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if candidate.get("delegation_id") == delegation_id:
                return candidate
            others.append(candidate)
    finally:
        for other in others:
            process_registry.completion_queue.put(other)
    raise AssertionError(f"no completion for {delegation_id}")


@pytest.mark.parametrize("origin", [
    {"session_key": "", "origin_ui_session_id": "tui-tab-1"},
    {"session_key": "agent:main:api_server:dm:1"},
    {"session_key": ""},
], ids=["tui", "api_server", "cli"])
@pytest.mark.parametrize("cut_reason", ["gateway shutdown (restart)", "stopped before finishing"])
def test_scalar_cut_notice_admits_one_recovery_from_current_state(origin, cut_reason):
    started, release = threading.Event(), threading.Event()

    def runner():
        started.set()
        assert release.wait(5)
        return {"results": [{"task_index": 0, "status": "interrupted", "summary": "partial work"}]}

    try:
        handle = ad.dispatch_async_delegation_batch(
            goals=["only task"], context="original context", toolsets=None, role="leaf", model="m",
            parent_session_id="owner", runner=runner, interrupt_fn=lambda reason=None: release.set(),
            max_async_children=1, **origin,
        )
        assert handle["accepted"] and started.wait(5)
        assert ad.interrupt_all(cut_reason) == 1
        did = handle["delegation_id"]
        event = _await_completion(did)
        assert dr.retry_status(did)["retry_state"] == "none"
        text = _format_async_delegation(event)
        assert f"delegate_task(action='resume', subagent_id='{did}')" in text
        payload = json.loads(_handle_control_action("resume", did, None, Parent()))
        assert payload.get("status") == "recovery_claimed", payload
        assert payload["goal"] == "only task"
        assert "tasks" not in payload
        assert "MUST verify current state" in payload["recovery_context"]
        assert "original context" in payload["recovery_context"]
        if cut_reason == "stopped before finishing":
            assert "CLI exit" in text
            assert "gateway" not in text.lower()
        # Rendering is read-only, and a re-offered copy (restamped off-loop on replay or claim) must
        # not promise a spent claim, including a retry note serialized before the claim was spent.
        event["retry_note"] = f"To continue call delegate_task(action='resume', subagent_id='{did}')."
        dr.stamp_resume_availability(event)
        assert event["resume_available"] is False
        assert "action='resume'" not in _format_async_delegation(event)
        assert json.loads(_handle_control_action("resume", did, None, Parent())).get("error")
        with ad._DB_LOCK, ad._transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM async_delegation_child_recovery").fetchone()[0] == 0
        # The scalar recovery_context remains admissible as a normal replacement.
        ad._persist_dispatch(dict(
            delegation_id="deleg_scalar_replacement", goal=payload["goal"],
            context=payload["recovery_context"], parent_session_id="owner", dispatched_at=time.time(),
            **origin,
        ))
        assert dr.retry_status("deleg_scalar_replacement") is not None
    finally:
        release.set()


@pytest.mark.parametrize("child_status", ["completed", "success", "succeeded"])
def test_successful_scalar_child_is_not_resumable_even_in_an_interrupted_row(child_status):
    did, now = "deleg_scalar_success", time.time()
    ad._persist_dispatch(dict(
        delegation_id=did, goal="only task", goals=["only task"], is_batch=True,
        context="ctx", session_key="", parent_session_id="owner", dispatched_at=now,
    ))
    event = dict(type="async_delegation", delegation_id=did, goal="only task", goals=["only task"],
                 is_batch=True, status="interrupted", cut_reason="gateway shutdown (restart)",
                 results=[{"task_index": 0, "status": child_status, "summary": "done"}], completed_at=now)
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET state='interrupted', result_json=?, event_json=? "
                     "WHERE delegation_id=?", (json.dumps(event), json.dumps(event), did))
    _, reason = dr.inspect_resumable(did)
    assert reason is not None
    assert json.loads(_handle_control_action("resume", did, None, Parent())).get("error")
    dr.stamp_resume_availability(event)
    assert "action='resume'" not in _format_async_delegation(event)
    record = dr.retry_status(did)
    assert record["resume_state"] == "none" and record["resume_attempts"] == 0
