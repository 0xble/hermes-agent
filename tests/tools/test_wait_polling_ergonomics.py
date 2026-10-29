"""Bounded waits and session-scoped delegation observations, through tool dispatch.

Invariant: a short default and end-the-turn guidance appear only when a completion
notice will actually arrive (notify_on_complete AND the session can receive it later).
"""

import contextvars
import json
from types import SimpleNamespace

import pytest

from tools import delegate_tool_registry as delegation
from tools import process_registry as processes
from tools.delegate_tool import delegate_task
from tools.registry import registry

CEILING = 180


def _timed_out_wait(monkeypatch, *, notify, requested=None, owner_task_id="", parent_session_id="", session_key=""):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(processes, "_monotonic", lambda: clock.now)
    monkeypatch.setenv("TERMINAL_TIMEOUT", str(CEILING))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
    monkeypatch.setattr("tools.interrupt.consume_yield", lambda *_: False)

    class Completion:
        def wait(self, timeout):
            clock.now += timeout

    store = processes.ProcessRegistry()
    session = processes.ProcessSession(
        id="proc_bounded_wait", command="bounded-work", task_id="owner", started_at=1,
        owner_task_id=owner_task_id, parent_session_id=parent_session_id, session_key=session_key,
        notify_on_complete=notify,
    )
    session._completion_event = Completion()
    store._running[session.id] = session
    monkeypatch.setattr(processes, "process_registry", store)
    args = {"action": "wait", "session_id": session.id}
    if requested is not None:
        args["timeout"] = requested
    result = json.loads(registry.dispatch("process_manage", args))
    assert result["status"] == "timeout" and result["process_running"] is True
    assert session.exited is False
    return clock.now, result["timeout_note"].lower()


def _assert_must_collect(waited, note):
    assert waited == CEILING
    assert "end the turn" not in note and "do not wait again" not in note
    assert "wait again with a bounded timeout" in note


def test_notified_and_reachable_gets_short_default_and_end_turn(monkeypatch):
    waited, note = _timed_out_wait(monkeypatch, notify=True)
    assert waited == 30
    assert "end the turn" in note and "do not wait again" in note


def test_explicit_timeout_is_clamped_to_ceiling(monkeypatch):
    waited, note = _timed_out_wait(monkeypatch, notify=True, requested=999)
    assert waited == CEILING
    assert "clamped" in note


def test_without_notify_keeps_full_default_and_wait_again(monkeypatch):
    # Nothing will ever notify this process, so deferring would lose its result.
    _assert_must_collect(*_timed_out_wait(monkeypatch, notify=False))


@pytest.mark.parametrize("notify", [True, False])
def test_stateless_session_keeps_full_default_and_wait_again(monkeypatch, notify):
    from gateway.session_context import declare_stateless_channel

    def run():
        declare_stateless_channel()
        return _timed_out_wait(monkeypatch, notify=notify)

    _assert_must_collect(*contextvars.copy_context().run(run))


@pytest.mark.parametrize("notify", [True, False])
def test_delegated_child_keeps_full_default_and_may_hand_off(monkeypatch, notify):
    from agent.delegation_context import delegated_child_context

    with delegated_child_context(session_id="child-session", parent_session_id="parent-session"):
        waited, note = _timed_out_wait(monkeypatch, notify=notify)
    _assert_must_collect(waited, note)
    assert "action='handoff'" in note


def test_subagent_owned_process_keeps_full_default_and_wait_again(monkeypatch):
    waited, note = _timed_out_wait(
        monkeypatch, notify=True, owner_task_id="sa-child", parent_session_id="child-session",
    )
    _assert_must_collect(waited, note)


def test_yield_release_uses_full_wait_guidance_for_unroutable_process(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(processes, "_monotonic", lambda: clock.now)
    monkeypatch.setenv("TERMINAL_TIMEOUT", str(CEILING))
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
    monkeypatch.setattr("tools.interrupt.consume_yield", lambda *_: True)
    store = processes.ProcessRegistry()
    session = processes.ProcessSession(
        id="proc_yield_unroutable", command="bounded-work", task_id="owner", owner_task_id="sa-child",
        parent_session_id="child-session", notify_on_complete=True,
    )
    store._running[session.id] = session
    monkeypatch.setattr(processes, "process_registry", store)
    raw = registry.dispatch("process_manage", {"action": "wait", "session_id": session.id})
    result = json.loads(raw) if isinstance(raw, str) else raw
    assert result["status"] == "interrupted"
    note = result["note"].lower()
    assert "wait again with a bounded timeout" in note
    assert "will not be routed to this session" in note
    assert "you will be notified on exit" not in note


@pytest.fixture
def observations(monkeypatch):
    from tools import async_delegation, delegation_resume

    monkeypatch.setattr(delegation, "_active_subagents", {})
    monkeypatch.setattr(async_delegation, "list_async_delegations", lambda: [])
    monkeypatch.setattr(delegation_resume, "list_retry_pending", lambda: [])
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(delegation, "_now", lambda: clock.now)
    return clock


def _list(parent):
    return json.loads(delegate_task(action="list", parent_agent=parent))


def _child(owner, sid="child"):
    return {
        "subagent_id": sid, "owner_agent_session_id": owner.session_id,
        "goal": "bounded work", "status": "running", "started_at": 950.0,
        "tool_count": 0, "agent": SimpleNamespace(_live_transcript_path="child.log"),
    }


def test_list_ignores_elapsed_time_but_reports_changed_state(observations):
    owner = SimpleNamespace(session_id="owner")
    child = _child(owner)
    delegation._register_subagent(child)
    assert _list(owner)["subagents"][0]["subagent_id"] == "child"
    observations.now += 60
    unchanged = _list(owner)
    assert unchanged["status"] == "unchanged" and unchanged["unchanged_since"] == 1000.0
    assert unchanged["subagent_ids"] == ["child"] and "subagents" not in unchanged
    child["tool_count"] = 1
    assert _list(owner)["subagents"][0]["tool_count"] == 1
    delegation._unregister_subagent("child")
    assert _list(owner)["count"] == 0
    empty = _list(owner)
    assert empty["status"] == "unchanged" and "end the turn" not in empty["note"].lower()


@pytest.mark.parametrize("reachable", [True, False])
def test_unchanged_list_says_end_turn_only_when_results_can_arrive(observations, monkeypatch, reachable):
    from tools import delegate_tool_progress

    monkeypatch.setattr(delegate_tool_progress, "can_receive_later", lambda: reachable)
    owner = SimpleNamespace(session_id="owner")
    delegation._register_subagent(_child(owner))
    _list(owner)
    note = _list(owner)["note"].lower()
    assert "unchanged since" in note
    assert ("end the turn" in note) is reachable


def test_list_observations_are_session_scoped_and_include_queued_changes(observations, monkeypatch):
    from tools import async_delegation

    owner = SimpleNamespace(session_id="owner")
    other = SimpleNamespace(session_id="other")
    assert _list(owner)["count"] == 0
    assert _list(owner)["status"] == "unchanged"
    assert _list(other)["count"] == 0
    owner.session_id = "new-session"
    assert _list(owner)["count"] == 0
    queued = {
        "delegation_id": "queued", "parent_session_id": "new-session",
        "status": "queued", "queued_at": 950.0, "goal": "queued work",
    }
    monkeypatch.setattr(async_delegation, "list_async_delegations", lambda: [queued])
    assert _list(owner)["subagents"][0]["status"] == "queued"
    observations.now += 60
    assert _list(owner)["status"] == "unchanged"
    queued["queue_reason"] = "different reason"
    assert _list(owner)["subagents"][0]["queue_reason"] == "different reason"


def test_unchanged_list_keeps_retry_rows_and_retry_metadata_visible(observations, monkeypatch):
    from tools import delegation_resume

    owner = SimpleNamespace(session_id="owner")
    retry_rows = [
        {
            "delegation_id": "retry-due", "parent_session_id": "owner", "retry_state": "notified",
            "retry_reason": "child exited before delivering", "retry_due_at": 1234.5,
            "retry_attempt": 2, "task": {"goal": "recover this work"},
        },
        {
            "delegation_id": "retry-terminal", "parent_session_id": "owner", "retry_state": "terminal",
            "retry_reason": "retry budget exhausted", "retry_due_at": None,
            "retry_attempt": 3, "task": {"goal": "report the terminal outcome"},
        },
    ]
    monkeypatch.setattr(delegation_resume, "list_retry_pending", lambda: retry_rows)
    from tools import delegate_tool_progress
    monkeypatch.setattr(delegate_tool_progress, "can_receive_later", lambda: True)
    first = _list(owner)
    assert {entry["status"] for entry in first["subagents"]} == {"retry_notified", "retry_terminal"}
    unchanged = _list(owner)
    assert unchanged["status"] == "unchanged"
    assert "end the turn" not in unchanged["note"].lower()
    assert {entry["status"] for entry in unchanged["subagents"]} == {"retry_notified", "retry_terminal"}
    due = next(entry for entry in unchanged["subagents"] if entry["subagent_id"] == "retry-due")
    assert due["retry_reason"] == "child exited before delivering"
    assert due["retry_due_at"] == 1234.5
