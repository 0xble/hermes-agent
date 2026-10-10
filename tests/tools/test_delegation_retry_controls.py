"""Cancellation, consumed recovery claims, and retry-driver origin admission."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tools import async_delegation as ad
from tools import delegate_tool_registry as registry
from tools import delegation_resume as dr
from tools.delegate_tool_child_run import _SchemaOutcome, _build_result_entry
from tests.tools.test_delegation_retry import T0, _dispatch, _ledger, _rate_limited, _row, _terminal


@pytest.fixture(autouse=True)
def _control_state(monkeypatch):
    ad._reset_for_tests()
    monkeypatch.setattr(registry, "_active_subagents", {})
    monkeypatch.setattr(dr.time, "time", lambda: T0 + 3600)
    yield
    ad._reset_for_tests()


def _scheduled(did="deleg_stop_pending"):
    _dispatch(did)
    _rate_limited(did)
    dr.schedule_retry(did, now=T0 + 60)
    return did


class _Child:
    def hard_interrupt(self, message=None, **kwargs):
        self.interrupt_message = message


@pytest.mark.parametrize("producer", ["direct", "model", "compat"])
@pytest.mark.parametrize("state", ["interrupted", "error"])
def test_explicit_live_child_stop_is_not_retryable(producer, state):
    child = _Child()
    registry._active_subagents["child"] = {
        "agent": child, "owner_agent_session_id": "sess-owner"}
    if producer == "direct":
        assert registry.interrupt_subagent("child")
    elif producer == "compat":
        from agent.interrupt_compat import request_hard_interrupt
        assert request_hard_interrupt(child)
    else:
        result = json.loads(registry._handle_control_action(
            "stop", "child", None, SimpleNamespace(session_id="sess-owner")))
        assert result["status"] == "interrupt_requested"
    entry = _build_result_entry(
        child, {"interrupted": True, "final_response": "Operation interrupted.", "messages": []},
        0, 1.0, _SchemaOutcome(None, None, [], 0))
    assert entry.get("interrupt_reason") == ("cancel" if producer == "compat" else "stop_command")
    did = _dispatch("deleg_live_stop")
    _terminal(did, state, results=[entry])
    assert dr.classify_outcome(dr.retry_status(did))[0] == dr.RETRY_NONE


@pytest.mark.parametrize("state", ["interrupted", "error"])
def test_shutdown_child_remains_retryable(state):
    from tools.delegate_tool_child_run import _signal_child_stop
    child = _Child()
    _signal_child_stop(child, "gateway shutdown (final-cleanup)")
    entry = _build_result_entry(child, {"interrupted": True, "final_response": "", "messages": []},
                                0, 1.0, _SchemaOutcome(None, None, [], 0))
    did = _dispatch("deleg_system_stop")
    _terminal(did, state, results=[entry])
    assert dr.classify_outcome(dr.retry_status(did))[0] == "retry"


@pytest.mark.parametrize("producer", ["individual", "model", "session", "all"])
def test_explicit_stop_cancels_retry_without_live_child(producer):
    did = _scheduled()
    other = _dispatch("deleg_other_chat")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET origin_session='agent:main:telegram:dm:2', "
                     "parent_session_id='other-parent' WHERE delegation_id=?", (other,))
    _rate_limited(other)
    dr.schedule_retry(other, now=T0 + 60)
    if producer == "individual":
        assert ad.interrupt_delegation(did, reason="stop_command")
    elif producer == "model":
        result = json.loads(registry._handle_control_action(
            "stop", did, None, SimpleNamespace(session_id="sess-owner")))
        assert result["status"] == "interrupt_requested"
    elif producer == "session":
        assert ad.interrupt_for_session(session_key="agent:main:telegram:dm:1", reason="user_stop") == 1
    else:
        assert ad.interrupt_all(reason="stop_command") == 2
    assert _row(did)["retry_state"] == "cancelled"
    assert dr.claim_retry_dispatch(did)[1] is not None
    actions = dr.sweep_retries(now=T0 + 7200)
    assert all(a["delegation_id"] != did for a in actions)
    if producer != "all":
        assert _row(other)["retry_state"] != "cancelled"


def test_cancelled_retry_clears_stale_completion_promise():
    did = _scheduled()
    event = {"type": "async_delegation", "delegation_id": did, "retry_note": "Hermes will recover it automatically"}
    assert ad.interrupt_delegation(did)
    ad._attach_retry_note(event)
    assert "retry_note" not in event


def test_stop_cannot_cancel_another_conversations_retry():
    did = _scheduled()
    result = json.loads(registry._handle_control_action(
        "stop", did, None, SimpleNamespace(session_id="stranger")))
    assert "error" in result
    assert _row(did)["retry_state"] == "scheduled"


def test_shutdown_preserves_pending_retry():
    did = _scheduled()
    assert ad.interrupt_all(reason="shutdown") == 0
    assert _row(did)["retry_state"] == "scheduled"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notice", "terminal"])
async def test_cancellation_invalidates_collected_retry_action(kind):
    from tests.gateway.test_delegation_retry_delivery import _runner

    did = _scheduled()
    if kind == "terminal":
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET retry_state='terminal' WHERE delegation_id=?", (did,))
    (action,) = dr.sweep_retries(now=T0 + 3600)
    assert action["kind"] == kind
    assert ad.interrupt_delegation(did)
    runner = _runner()
    await runner._deliver_delegation_retry_action(action)
    runner._deliver_platform_notice.assert_not_awaited()
    runner._inject_watch_notification.assert_not_awaited()
    assert _row(did)["retry_state"] == "cancelled"


@pytest.mark.parametrize("concurrent", [False, True])
def test_consumed_recovery_brief_cannot_create_fresh_root(concurrent):
    did = _scheduled("deleg_one_claim")
    record, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    brief = dr.build_recovery_instruction(record)

    def replacement(new_id):
        try:
            _dispatch(new_id, context=brief, dispatched_at=T0 + 3600)
            return True
        except RuntimeError:
            return False

    ids = ["deleg_one_replacement", "deleg_duplicate_replacement"]
    if concurrent:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(replacement, ids))
    else:
        results = [replacement(i) for i in ids]
    assert sorted(results) == [False, True]
    winner = _row(did)["retry_replacement"]
    assert _row(winner)["retry_root"] == did
    assert _row(winner)["retry_attempt"] == 1
    assert _row(next(i for i in ids if i != winner)) is None


@pytest.mark.parametrize("session_key,ui_sid", [
    ("", "tui-tab"), ("cli:parent", ""),
    ("agent:main:local:dm:1", ""), ("agent:main:tui:dm:1", "tui-tab"),
    ("agent:main:api_server:dm:1", "tui-tab"),
])
def test_unsupported_origin_retains_main_resume_without_automatic_promise(session_key, ui_sid):
    did = "deleg_no_gateway_driver"
    ad._persist_dispatch({
        "delegation_id": did, "goal": "task", "goals": ["task"], "is_batch": True,
        "context": "ctx", "role": "leaf", "model": "m", "session_key": session_key,
        "origin_ui_session_id": ui_sid, "origin_session_id": "parent", "parent_session_id": "parent",
        "dispatched_at": T0})
    assert _row(did)["retry_state"] == "none"
    _rate_limited(did)
    event = {"type": "async_delegation", "delegation_id": did}
    ad._attach_retry_note(event)
    assert "retry_note" not in event
    assert dr.sweep_retries(now=T0 + 7200) == []


@pytest.mark.parametrize("platform", ["telegram", "slack", "discord"])
def test_gateway_origin_is_armed(platform):
    did = "deleg_gateway_driver"
    ad._persist_dispatch({
        "delegation_id": did, "goal": "task", "goals": ["task"], "is_batch": True,
        "context": "ctx", "role": "leaf", "model": "m",
        "session_key": f"agent:main:{platform}:dm:1", "origin_ui_session_id": "",
        "origin_session_id": "gateway-wake", "parent_session_id": "parent", "dispatched_at": T0})
    assert _row(did)["retry_state"] == "armed"


@pytest.mark.parametrize("session_key,ui_sid,retry_state", [
    ("agent:main:cli:dm:1", "", "none"),
    ("", "tui-tab", "none"),
    ("agent:main:telegram:dm:1", "", "none"),
])
def test_claimed_resume_admits_first_replacement_and_only_one(session_key, ui_sid, retry_state):
    """The recovery claim authorizes the replacement spawn it instructed."""
    did = "deleg_legacy_resume_claim"
    ad._persist_dispatch({
        "delegation_id": did, "goal": "recover task", "goals": None, "is_batch": False,
        "context": "ctx", "role": "leaf", "model": "m", "session_key": session_key,
        "origin_ui_session_id": ui_sid, "origin_session_id": "parent", "parent_session_id": "parent",
        "dispatched_at": T0,
    })
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET state='interrupted', retry_state=? WHERE delegation_id=?",
                     (retry_state, did))
    record, reason = dr.claim_resume(did)
    assert reason is None
    assert record is not None
    brief = dr.build_recovery_instruction(record)

    def replacement_record(replacement_id):
        return {
            "delegation_id": replacement_id, "goal": "recover task", "goals": None, "is_batch": False,
            "context": brief, "role": "leaf", "model": "m", "session_key": session_key,
            "origin_ui_session_id": ui_sid, "origin_session_id": "parent", "parent_session_id": "parent",
            "dispatched_at": T0 + 3600,
        }

    replacement = "deleg_legacy_resume_replacement"
    ad._persist_dispatch(replacement_record(replacement))
    assert _row(did)["retry_replacement"] == replacement
    with pytest.raises(RuntimeError, match="recovery claim already consumed"):
        ad._persist_dispatch(replacement_record("deleg_duplicate_resume_replacement"))


def test_live_interrupt_survives_locked_retry_cancellation(monkeypatch):
    """A durable cancellation failure must not prevent the live child signal."""
    interrupted = threading.Event()
    release = threading.Event()

    def fail_cancel(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(dr, "cancel_pending_retries", fail_cancel)
    handle = ad.dispatch_async_delegation(
        goal="live child", context=None, toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:1", parent_session_id="parent",
        interrupt_fn=lambda reason=None: interrupted.set(),
        runner=lambda: (release.wait(5), {"status": "completed"})[1], max_async_children=1,
    )
    assert handle["status"] == "dispatched"
    assert ad.interrupt_delegation(handle["delegation_id"], reason="stop_command") is True
    assert interrupted.wait(2)
    release.set()
    assert _row(handle["delegation_id"])["state"] in {"queued", "running", "interrupted", "cancelled"}
