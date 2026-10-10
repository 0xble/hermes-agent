"""Durable, bounded automatic retry of background delegations.

Guarantee under test: every background delegation ends either delivered as a
completion or as ONE visible terminal line — never silently abandoned, whatever
the parent model replies to a notice.
"""

import json
import sqlite3
import threading
import time

import pytest

from tools import async_delegation as ad
from tools import delegation_resume as dr


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    yield


T0 = 1_800_000_000.0


def _dispatch(delegation_id, *, goal="build the model", dispatched_at=None, goals=None, lineage=None, context="ctx"):
    record = {
        "delegation_id": delegation_id, "goal": goal, "goals": goals or [goal], "is_batch": True,
        "context": context, "toolsets": None, "role": "leaf", "model": "m",
        "session_key": "agent:main:telegram:dm:1", "origin_ui_session_id": "",
        "origin_session_id": "", "parent_session_id": "sess-owner",
        "dispatched_at": dispatched_at or T0,
        "task_transcripts": {"0": f"/live/{delegation_id}/task-0.log"},
    }
    ad._persist_dispatch(record)
    if lineage:
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET retry_root=?, retry_attempt=?, retry_root_started_at=? "
                         "WHERE delegation_id=?", (lineage["root"], lineage["attempt"], lineage["root_started_at"],
                                                   delegation_id))
    return delegation_id


def _redispatch(record, new_id, dispatched_at=None):
    """What the parent does with the resume brief: a normal spawn whose context is the brief."""
    return _dispatch(new_id, dispatched_at=dispatched_at, context=dr.build_recovery_instruction(record))


def _terminal(delegation_id, state, *, results=None, error=None, interrupt_reason=None, at=T0 + 60):
    event = {"type": "async_delegation", "delegation_id": delegation_id, "status": state, "error": error}
    if interrupt_reason:
        event["interrupt_reason"] = interrupt_reason
    result = {"results": results or [], "error": error}
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET state=?, completed_at=?, updated_at=?, event_json=?, result_json=?, "
            "delivery_state='delivered' "
            "WHERE delegation_id=?", (state, at, at, json.dumps(event), json.dumps(result), delegation_id))


def _rate_limited(delegation_id, at=T0 + 60):
    _terminal(delegation_id, "error", at=at, results=[{
        "task_index": 0, "status": "failed", "failure_reason": "rate_limit",
        "error": "HTTP 429: All credentials for model claude-opus-5-5 are cooling down",
        "summary": "Limit resets in 26m."}])


def _row(delegation_id):
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.row_factory = None
        cols = [c[1] for c in conn.execute("PRAGMA table_info(async_delegations)")]
        row = conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
    return dict(zip(cols, row)) if row else None


@pytest.mark.parametrize("shape", ["tasks", "legacy"])
def test_resume_context_links_through_model_dispatch_unit(monkeypatch, shape):
    from types import SimpleNamespace
    from tools.delegate_tool_dispatch import _dispatch_unit

    ad._reset_for_tests()
    monkeypatch.setattr(dr.time, "time", lambda: T0 + 3600)
    did = _dispatch("deleg_model_shape")
    _rate_limited(did)
    dr.schedule_retry(did, now=T0 + 60)
    record, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    brief = dr.build_recovery_instruction(record)
    task = {"goal": "build the model"}
    if shape == "tasks":
        task["context"] = brief
    unit = SimpleNamespace(task_list=[task], context=brief if shape == "legacy" else None,
                           children=[(0, task, object())], top_role="leaf", creds={"model": "m"}, live_writers=[])
    try:
        handle = _dispatch_unit(unit, "deleg_model_replacement", None, {
            "session_key": "agent:main:telegram:dm:1", "parent_session_id": "sess-owner",
            "max_async_children": 0, "max_queued_delegations": 1})
        assert handle["status"] == "queued"
        replacement = _row("deleg_model_replacement")
        assert replacement["retry_attempt"] == 1
        assert replacement["retry_root"] == did
        assert replacement["retry_root_started_at"] == T0
        assert _row(did)["retry_state"] == "dispatched"
        assert _row(did)["retry_replacement"] == "deleg_model_replacement"
        assert json.loads(replacement["task_json"])["context"] == brief
    finally:
        ad._reset_for_tests()


def test_retry_scheduled_at_one_hour_swept_after_48_hours_is_terminal():
    did = _dispatch("deleg_offline")
    _rate_limited(did)
    assert dr.schedule_retry(did, now=T0 + 3600)["retry_state"] == "scheduled"
    (action,) = dr.sweep_retries(now=T0 + 48 * 3600)
    assert action["kind"] == "terminal"
    assert "24h" in action["text"]


def test_resume_claim_rechecks_lineage_deadline(monkeypatch):
    did = _dispatch("deleg_expired_claim")
    _rate_limited(did)
    dr.schedule_retry(did, now=T0 + 3600)
    monkeypatch.setattr(dr.time, "time", lambda: T0 + 48 * 3600)
    record, reason = dr.claim_retry_dispatch(did)
    assert reason is not None
    assert _row(did)["retry_state"] == "terminal"
    assert "24h" in record["retry_reason"]


def test_dispatch_after_claim_rechecks_lineage_deadline(monkeypatch):
    ad._reset_for_tests()
    did = _dispatch("deleg_expired_dispatch")
    _rate_limited(did)
    dr.schedule_retry(did, now=T0 + 3600)
    monkeypatch.setattr(dr.time, "time", lambda: T0 + 3600)
    record, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    monkeypatch.setattr(dr.time, "time", lambda: T0 + 48 * 3600)
    started = threading.Event()
    try:
        handle = ad.dispatch_async_delegation_batch(
            goals=["build the model"], context=dr.build_recovery_instruction(record),
            toolsets=None, role="leaf", model="m", session_key="agent:main:telegram:dm:1",
            parent_session_id="sess-owner", runner=lambda: started.set(),
            delegation_id="deleg_expired_replacement", max_async_children=0, max_queued_delegations=1)
        assert handle["accepted"] is False
        assert not started.is_set()
        assert _row("deleg_expired_replacement") is None
        assert _row(did)["retry_state"] == "terminal"
    finally:
        ad._reset_for_tests()


def test_expired_model_dispatch_does_not_fall_back_to_inline_execution(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from tools import delegate_tool_dispatch as dispatch
    from tools import delegate_tool

    ad._reset_for_tests()
    did = _dispatch("deleg_model_deadline")
    _rate_limited(did)
    dr.schedule_retry(did, now=T0 + 3600)
    monkeypatch.setattr(dr.time, "time", lambda: T0 + 3600)
    record, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    task = {"goal": "build the model", "context": dr.build_recovery_instruction(record)}
    batch = SimpleNamespace(task_list=[task], context=None, children=[(0, task, object())],
                            parent_agent=SimpleNamespace(session_id="sess-owner"),
                            creds={"model": "m"}, top_role="leaf", live_writers=[], live_paths=[],
                            live_deleg_id="deleg_expired_model", origin_wake_sid="wake",
                            origin_session_history_delivery=True, origin_ui_session_id="")
    monkeypatch.setattr(dispatch, "_resolve_async_session_key", lambda *_: ("agent:main:telegram:dm:1", ""))
    monkeypatch.setattr(dispatch, "_units_of", lambda b: [b])
    monkeypatch.setattr(dispatch, "_detach_child", Mock())
    monkeypatch.setattr(dispatch, "_cleanup_unit", Mock())
    monkeypatch.setattr(dispatch, "_run_sync_with_note", Mock(side_effect=AssertionError("expired retry ran inline")))
    monkeypatch.setattr(delegate_tool, "_get_max_async_children", lambda: 0)
    monkeypatch.setattr(delegate_tool, "_get_max_queued_delegations", lambda: 1)
    monkeypatch.setattr(dr.time, "time", lambda: T0 + 48 * 3600)
    try:
        result = json.loads(dispatch._dispatch_background(batch))
        assert result["status"] == "rejected"
        assert "24h" in result["error"]
        assert _row("deleg_expired_model") is None
        assert _row(did)["retry_state"] == "terminal"
        dispatch._run_sync_with_note.assert_not_called()
    finally:
        ad._reset_for_tests()


def test_managed_retry_storage_failure_does_not_run_inline_or_consume_claim(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from tools import delegate_tool_dispatch as dispatch
    from tools import delegate_tool

    ad._reset_for_tests()
    did = _dispatch("deleg_storage_failure")
    _rate_limited(did)
    dr.schedule_retry(did, now=T0 + 3600)
    monkeypatch.setattr(dr.time, "time", lambda: T0 + 3600)
    record, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    task = {"goal": "build the model", "context": dr.build_recovery_instruction(record)}
    batch = SimpleNamespace(task_list=[task], context=None, children=[(0, task, object())],
                            parent_agent=SimpleNamespace(session_id="sess-owner"),
                            creds={"model": "m"}, top_role="leaf", live_writers=[], live_paths=[],
                            live_deleg_id="deleg_storage_replacement", origin_wake_sid="wake",
                            origin_session_history_delivery=True, origin_ui_session_id="")
    monkeypatch.setattr(dispatch, "_resolve_async_session_key", lambda *_: ("agent:main:telegram:dm:1", ""))
    monkeypatch.setattr(dispatch, "_units_of", lambda b: [b])
    monkeypatch.setattr(dispatch, "_detach_child", Mock())
    monkeypatch.setattr(dispatch, "_cleanup_unit", Mock())
    inline = Mock(side_effect=AssertionError("managed retry ran inline"))
    monkeypatch.setattr(dispatch, "_run_sync_with_note", inline)
    monkeypatch.setattr(delegate_tool, "_get_max_async_children", lambda: 0)
    monkeypatch.setattr(delegate_tool, "_get_max_queued_delegations", lambda: 1)
    original_persist = ad._persist_dispatch

    def fail_replacement(new_record):
        if new_record["delegation_id"] == "deleg_storage_replacement":
            raise sqlite3.OperationalError("database is locked")
        return original_persist(new_record)

    monkeypatch.setattr(ad, "_persist_dispatch", fail_replacement)
    try:
        result = json.loads(dispatch._dispatch_background(batch))
        assert result["status"] == "rejected"
        assert "not started" in result["error"]
        assert _row("deleg_storage_replacement") is None
        prior = _row(did)
        assert prior["retry_state"] == "scheduled"
        assert prior["retry_claim"] in (None, "")
        assert prior["retry_due_at"] > T0 + 3600
        inline.assert_not_called()
    finally:
        ad._reset_for_tests()


def test_recovery_spawn_fails_closed_when_ledger_cannot_establish_ownership(monkeypatch):
    """Persistence AND the recovery-source lookup both fail: never run the recovery brief inline."""
    ad._reset_for_tests()
    did = _dispatch("deleg_ledger_down")
    _rate_limited(did)
    dr.schedule_retry(did, now=T0 + 3600)
    monkeypatch.setattr(dr.time, "time", lambda: T0 + 3600)
    record, reason = dr.claim_retry_dispatch(did)
    assert reason is None and record is not None
    context = dr.build_recovery_instruction(record)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ad, "_persist_dispatch", locked)
    monkeypatch.setattr(dr, "claimed_retry_source", locked)
    try:
        result = ad.dispatch_async_delegation(
            goal="build the model", context=context, toolsets=None, role="leaf", model="m",
            session_key="agent:main:telegram:dm:1", parent_session_id="sess-owner",
            origin_session_id="sess-owner", runner=lambda: {"status": "completed"},
            delegation_id="deleg_ledger_down_replacement")
        assert result["accepted"] is False
        assert result["no_inline_fallback"] is True
        assert "not started" in result["error"]
    finally:
        ad._reset_for_tests()


def test_queued_replacement_cannot_start_after_lineage_deadline(monkeypatch):
    ad._reset_for_tests()
    did = _dispatch("deleg_queue_deadline")
    _rate_limited(did)
    dr.schedule_retry(did, now=T0 + 3600)
    monkeypatch.setattr(dr.time, "time", lambda: T0 + 3600)
    record, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    started = threading.Event()
    try:
        handle = ad.dispatch_async_delegation_batch(
            goals=["build the model"], context=dr.build_recovery_instruction(record),
            toolsets=None, role="leaf", model="m", session_key="agent:main:telegram:dm:1",
            parent_session_id="sess-owner", runner=lambda: started.set(),
            delegation_id="deleg_queued_deadline", max_async_children=0, max_queued_delegations=1)
        assert handle["status"] == "queued"
        monkeypatch.setattr(dr.time, "time", lambda: T0 + 48 * 3600)
        with ad._records_lock:
            ad._records["deleg_queued_deadline"]["max_async_children"] = 1
        ad._admit_pending()
        assert not started.wait(0.1)
        assert _row("deleg_queued_deadline")["retry_state"] == "terminal"
        assert ad.list_async_delegations()[0]["status"] == "error"
    finally:
        ad._reset_for_tests()


def test_submission_deadline_check_reads_the_records_own_profile_ledger(tmp_path, monkeypatch):
    """Global queue admission can run while another profile is active; read the record's own ledger."""
    import contextvars
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    home_a, home_b = tmp_path / "a", tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()
    monkeypatch.setattr(ad, "_db_path", lambda: get_hermes_home() / "state.db")
    token = set_hermes_home_override(home_b)
    try:
        did = _dispatch("deleg_profile_b_expired", lineage={"root": "deleg_root_b", "attempt": 2,
                                                            "root_started_at": T0})
        ctx_b = contextvars.copy_context()
    finally:
        reset_hermes_home_override(token)
    monkeypatch.setattr(dr.time, "time", lambda: T0 + 48 * 3600)
    assert ctx_b.run(dr.retry_submission_error, did)  # B's own ledger says the lineage expired
    started = threading.Event()
    record = {"delegation_id": did, "is_batch": True, "_context": ctx_b,
              "crash_result": lambda reason, _n: {"error": reason}, "runner": started.set}
    finalized = []
    monkeypatch.setattr(ad, "_finalize", lambda d, result, status: finalized.append((d, status)))
    token = set_hermes_home_override(home_a)
    try:
        with ad._records_lock:
            reason = ad._submit_record(record, 1)
    finally:
        reset_hermes_home_override(token)
        ad._reset_for_tests()
    assert reason and "budget" in reason
    assert finalized == [(did, "error")]
    assert not started.is_set()


# (1) incident replay ---------------------------------------------------------------------------
def test_rate_limited_failure_schedules_durable_retry_honoring_reset():
    did = _dispatch("deleg_429")
    _rate_limited(did)
    plan = dr.schedule_retry(did, now=T0 + 60)
    assert plan["retry_state"] == "scheduled"
    assert plan["reason"] == "rate_limit"
    # "resets in 26m" wins over the 5-minute base backoff.
    assert plan["due_at"] >= T0 + 60 + 26 * 60
    row = _row(did)
    assert row["retry_state"] == "scheduled" and row["retry_due_at"] == plan["due_at"]


def test_incident_replay_survives_restart_dispatches_once_and_ignores_no_reply():
    did = _dispatch("deleg_incident")
    _rate_limited(did)
    plan = dr.schedule_retry(did, now=T0 + 60)
    assert dr.sweep_retries(now=T0 + 120) == []  # not due yet

    ad._reset_for_tests()  # simulated gateway restart: memory gone, ledger remains
    due = plan["due_at"] + 1
    actions = dr.sweep_retries(now=due)
    assert [a["kind"] for a in actions] == ["notice"]
    assert "action='resume'" in actions[0]["text"] and did in actions[0]["text"]
    assert dr.sweep_retries(now=due) == []  # claimed: a concurrent watcher gets nothing
    assert dr.mark_notice_accepted(did, actions[0]["claim"], now=due)

    # Parent replied NO_REPLY and dispatched nothing: after the grace window it is prompted once more.
    again = dr.sweep_retries(now=due + dr.NOTICE_GRACE_S + 1)
    assert [a["kind"] for a in again] == ["notice"]
    assert dr.mark_notice_accepted(did, again[0]["claim"], now=due + dr.NOTICE_GRACE_S + 1)

    # Now the parent calls resume: exactly one replacement, linked to the lineage.
    record, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    assert "task-0.log" in dr.build_recovery_instruction(record)
    _redispatch(record, "deleg_incident_r1")
    assert dr.claim_retry_dispatch(did)[1] is not None
    r1 = _row("deleg_incident_r1")
    assert r1["retry_root"] == did and r1["retry_attempt"] == 1
    assert _row(did)["retry_state"] == "dispatched"

    # The replacement completes normally: no further retry or terminal line.
    _terminal("deleg_incident_r1", "completed", results=[{"task_index": 0, "status": "completed", "summary": "ok"}])
    assert dr.schedule_retry("deleg_incident_r1", now=due + 7200) is None
    assert dr.sweep_retries(now=due + 10 * dr.NOTICE_GRACE_S) == []


def test_unanswered_retry_notice_becomes_visible_terminal_line():
    did = _dispatch("deleg_ignored")
    _rate_limited(did)
    plan = dr.schedule_retry(did, now=T0 + 60)
    now = plan["due_at"] + 1
    for _ in range(dr.MAX_RETRY_NOTICES):
        (action,) = dr.sweep_retries(now=now)
        assert action["kind"] == "notice"
        dr.mark_notice_accepted(did, action["claim"], now=now)
        now += dr.NOTICE_GRACE_S + 1
    (action,) = dr.sweep_retries(now=now)
    assert action["kind"] == "terminal"
    assert "build the model" in action["text"] and "\n" not in action["text"]
    assert dr.mark_terminal_reported(did, action["claim"])
    assert dr.sweep_retries(now=now + 10_000) == []


# (2) interrupt -> resume -> interrupt -> resume ------------------------------------------------
def test_shutdown_interrupt_chain_retries_each_lineage_hop():
    did = _dispatch("deleg_chain")
    _terminal(did, "interrupted", interrupt_reason="gateway shutdown (restart)")
    plan = dr.schedule_retry(did, now=T0 + 60)
    assert plan["retry_state"] == "scheduled" and plan["reason"] == "interrupted"
    record, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    _redispatch(record, "deleg_chain_r1", dispatched_at=T0 + 120)

    _terminal("deleg_chain_r1", "unknown", at=T0 + 300)  # owner died again (second restart)
    plan2 = dr.schedule_retry("deleg_chain_r1", now=T0 + 300)
    assert plan2["retry_state"] == "scheduled"
    record2, reason2 = dr.claim_retry_dispatch("deleg_chain_r1")
    assert reason2 is None
    _redispatch(record2, "deleg_chain_r2", dispatched_at=T0 + 400)
    r2 = _row("deleg_chain_r2")
    assert r2["retry_root"] == did and r2["retry_attempt"] == 2


def test_user_stop_is_not_retried_or_reported():
    did = _dispatch("deleg_stopped")
    _terminal(did, "interrupted", interrupt_reason="stop_command")
    assert dr.schedule_retry(did, now=T0 + 60) is None
    assert dr.sweep_retries(now=T0 + 100_000) == []


# (3) budget exhausted --------------------------------------------------------------------------
def test_attempt_budget_exhausted_is_terminal_and_visible_once():
    did = _dispatch("deleg_budget", lineage={"root": "deleg_root", "attempt": dr.MAX_RETRY_ATTEMPTS,
                                             "root_started_at": T0})
    _rate_limited(did)
    plan = dr.schedule_retry(did, now=T0 + 60)
    assert plan["retry_state"] == "terminal" and "budget" in plan["reason"]
    (action,) = dr.sweep_retries(now=T0 + 61)
    assert action["kind"] == "terminal"
    dr.mark_terminal_reported(did, action["claim"])
    assert dr.sweep_retries(now=T0 + 100_000) == []
    assert dr.claim_retry_dispatch(did)[1] is not None


def test_lineage_age_budget_exhausted_is_terminal():
    did = _dispatch("deleg_old", lineage={"root": "deleg_root", "attempt": 1, "root_started_at": T0},
                    dispatched_at=T0 + 25 * 3600)
    _rate_limited(did, at=T0 + 25 * 3600 + 60)
    plan = dr.schedule_retry(did, now=T0 + 25 * 3600 + 60)
    assert plan["retry_state"] == "terminal" and "budget" in plan["reason"]


# (4) non-retryable -----------------------------------------------------------------------------
@pytest.mark.parametrize("reason", ["content_policy_blocked", "auth_permanent", "format_error", "billing"])
def test_non_retryable_failure_is_terminal_with_one_line(reason):
    did = _dispatch(f"deleg_{reason}")
    _terminal(did, "error", results=[{"task_index": 0, "status": "failed", "failure_reason": reason,
                                      "error": "provider refused"}])
    plan = dr.schedule_retry(did, now=T0 + 60)
    assert plan["retry_state"] == "terminal"
    (action,) = dr.sweep_retries(now=T0 + 61)
    assert action["kind"] == "terminal" and "build the model" in action["text"]
    assert dr.claim_retry_dispatch(did)[1] is not None


def test_partial_fanout_is_terminal_visible():
    did = _dispatch("deleg_fan", goals=["a", "b"], goal="2 parallel subagents: a; b")
    _terminal(did, "completed", results=[{"task_index": 0, "status": "completed", "summary": "ok"},
                                         {"task_index": 1, "status": "failed", "failure_reason": "rate_limit"}])
    plan = dr.schedule_retry(did, now=T0 + 60)
    assert plan["retry_state"] == "terminal"


def test_success_is_untouched():
    did = _dispatch("deleg_ok")
    _terminal(did, "completed", results=[{"task_index": 0, "status": "completed", "summary": "ok"}])
    assert dr.schedule_retry(did, now=T0 + 60) is None
    assert _row(did)["retry_state"] == "none"


# (5) concurrent claim --------------------------------------------------------------------------
def test_concurrent_dispatch_claims_yield_single_winner():
    did = _dispatch("deleg_race")
    _rate_limited(did)
    dr.schedule_retry(did, now=T0 + 60)
    wins, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        record, reason = dr.claim_retry_dispatch(did)
        if reason is None:
            wins.append(record["retry_claim"])

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1


def test_concurrent_sweeps_yield_single_notice():
    did = _dispatch("deleg_sweep_race")
    _terminal(did, "unknown")
    plan = dr.schedule_retry(did, now=T0 + 60)
    out, barrier = [], threading.Barrier(4)

    def go():
        barrier.wait()
        out.extend(dr.sweep_retries(now=plan["due_at"] + 1))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == 1


# (6) pruning -----------------------------------------------------------------------------------
def test_pruning_never_removes_retry_pending_or_claimed_rows(monkeypatch):
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 2)
    old = time.time() - 30 * 24 * 3600
    keep = {}
    for name, state in (("sched", "scheduled"), ("notified", "notified"), ("dispatching", "dispatching"),
                        ("terminal", "terminal")):
        did = _dispatch(f"deleg_keep_{name}")
        _rate_limited(did, at=old)
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET retry_state=?, delivery_state='delivered', updated_at=? "
                         "WHERE delegation_id=?", (state, old, did))
        keep[did] = state
    # A row claimed by the legacy explicit-resume path (cause b of the incident).
    claimed = _dispatch("deleg_keep_claimed")
    _terminal(claimed, "interrupted", at=time.time())
    dr.schedule_retry(claimed, now=time.time())
    dr.claim_retry_dispatch(claimed)
    for i in range(6):
        did = _dispatch(f"deleg_filler_{i}")
        _terminal(did, "completed", at=time.time())
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET delivery_state='delivered' WHERE delegation_id=?", (did,))
    _dispatch("deleg_trigger_prune", dispatched_at=time.time())
    for did in [*keep, claimed]:
        assert _row(did) is not None, did


def test_real_failed_dispatch_is_classified_and_notice_says_hermes_retries():
    """End to end through the registry: the completion event carries the retry note (R5)."""
    from tools.process_registry import process_registry
    from tools.process_registry_notifications import _format_async_delegation

    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()

    def runner():
        return {"results": [{"task_index": 0, "status": "failed", "failure_reason": "rate_limit",
                             "error": "HTTP 429: All credentials cooling down"}], "total_duration_seconds": 1}

    handle = ad.dispatch_async_delegation_batch(
        goals=["build the model"], context="ctx", toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:1", parent_session_id="sess-owner", origin_session_id="",
        delegation_id="deleg_real_429", runner=runner, max_async_children=1)
    assert handle["status"] == "dispatched"
    evt = process_registry.completion_queue.get(timeout=5)
    assert evt["delegation_id"] == "deleg_real_429"
    assert "will retry this automatically" in evt["retry_note"]
    assert "Do not re-dispatch" in _format_async_delegation(evt)
    assert _row("deleg_real_429")["retry_state"] == "scheduled"
    ad._reset_for_tests()


def test_brief_from_another_conversation_or_without_claim_does_not_link():
    did = _dispatch("deleg_owned")
    _rate_limited(did)
    dr.schedule_retry(did, now=T0 + 60)
    record = dr.retry_status(did)
    _dispatch("deleg_unclaimed_copy", context=dr.build_recovery_instruction(record))
    assert _row(did)["retry_state"] == "scheduled"  # no resume claim yet: not linked
    record, _ = dr.claim_retry_dispatch(did)
    foreign = {"delegation_id": "deleg_foreign", "goal": "g", "goals": ["g"], "is_batch": True,
               "context": dr.build_recovery_instruction(record), "role": "leaf", "model": "m",
               "session_key": "agent:main:telegram:dm:999", "origin_ui_session_id": "", "origin_session_id": "",
               "parent_session_id": "sess-other", "dispatched_at": T0 + 200}
    ad._persist_dispatch(foreign)
    assert _row(did)["retry_state"] == "dispatching"
    assert _row("deleg_foreign")["retry_attempt"] == 0


@pytest.mark.parametrize("write_path", ["lifecycle", "fallback", "commit_then_raise", "deferred_fallback"])
@pytest.mark.parametrize("failure_reason,action_kind", [("rate_limit", "notice"), ("billing", "terminal")])
def test_owned_failure_retry_waits_for_exactly_once_completion_delivery(monkeypatch, write_path,
                                                                      failure_reason, action_kind):
    """All terminal ownership paths classify the same failure, without racing its replay."""
    import queue
    from tools.process_registry import process_registry

    monkeypatch.setattr(dr.time, "time", lambda: T0 + 60)
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    did = _dispatch("deleg_owned_failure")
    assert ad._persist_transition(did, "queued", "admitted") == 1
    assert ad._persist_transition(did, "admitted", "running") == 1
    record = {**json.loads(_row(did)["task_json"]), "delegation_id": did,
              "parent_session_id": "sess-owner", "session_key": "agent:main:telegram:dm:1",
              "dispatched_at": T0, "completed_at": T0 + 60, "_durable_state": "running"}
    result = {"results": [{"task_index": 0, "status": "failed", "failure_reason": failure_reason,
                           "error": "synthetic provider failure"}]}
    persist, outbox = ad._persist_completion, ad._persist_outbox_event

    def failed_write(*args, **kwargs):
        raise RuntimeError("synthetic pre-commit failure")

    def ambiguous_write(*args, **kwargs):
        persist(*args, **kwargs)
        raise RuntimeError("synthetic post-commit failure")

    if write_path != "lifecycle":
        monkeypatch.setattr(ad, "_persist_completion",
                            ambiguous_write if write_path == "commit_then_raise" else failed_write)
    if write_path == "deferred_fallback":
        monkeypatch.setattr(ad, "_persist_outbox_event", failed_write)
    ad._push_completion_event(record, result, "error")
    event = q.get_nowait()
    assert q.empty()
    if write_path == "deferred_fallback":
        assert dr.retry_status(did)["retry_state"] == "armed"
        monkeypatch.setattr(ad, "_persist_outbox_event", outbox)
    claim = ad.claim_event_delivery(event, "test-parent")
    assert claim
    plan = dr.retry_status(did)
    assert plan["retry_state"] == ("scheduled" if action_kind == "notice" else "terminal")
    assert event["retry_note"]
    assert bool(event.get("_delivery_event_id")) == (write_path in {"fallback", "deferred_fallback"})
    due = (plan["retry_due_at"] or T0 + 60) + 1
    # Another watcher must not produce a retry/report while the original event is pending/claimed.
    assert dr.sweep_retries(now=due) == []
    ad.complete_event_delivery(event, claim)
    assert ad.claim_event_delivery(dict(event), "duplicate-consumer") is None
    replay = queue.Queue()
    assert ad.restore_undelivered_completions(replay) == 0
    (action,) = dr.sweep_retries(now=due)
    assert action["kind"] == action_kind
    assert dr.sweep_retries(now=due) == []


def test_retry_waits_for_pending_interim_failure_notice():
    did = _dispatch("deleg_interim")
    _rate_limited(did)
    notice = {"type": "async_delegation", "delegation_id": did, "task_failure_notice": True,
              "results": [{"task_index": 0, "status": "failed", "failure_reason": "rate_limit"}]}
    ad._persist_outbox_event(notice, None, event_kind="task_failure")
    plan = dr.schedule_retry(did, now=T0 + 60)
    assert dr.sweep_retries(now=plan["due_at"] + 1) == []
    claim = ad.claim_event_delivery(notice, "parent")
    assert claim
    ad.complete_event_delivery(notice, claim)
    (action,) = dr.sweep_retries(now=plan["due_at"] + 1)
    assert action["kind"] == "notice"


@pytest.mark.parametrize("during_insert", [False, True])
def test_queued_shutdown_arms_retry_instead_of_user_cancellation(monkeypatch, during_insert):
    """Shutdown owns queued work even when it races the dispatch ledger insert."""
    ad._reset_for_tests()
    entered, release, started = threading.Event(), threading.Event(), threading.Event()
    original_persist = ad._persist_dispatch
    handles = {}

    def delayed_persist(record):
        entered.set()
        assert release.wait(5)
        original_persist(record)

    if during_insert:
        monkeypatch.setattr(ad, "_persist_dispatch", delayed_persist)
    dispatch = threading.Thread(target=lambda: handles.update(ad.dispatch_async_delegation(
        goal="shutdown recovery", context=None, toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:1", parent_session_id="sess-owner",
        runner=lambda: (started.set(), {"status": "completed"})[1],
        max_async_children=0, max_queued_delegations=1,
    )))
    dispatch.start()
    try:
        if during_insert:
            assert entered.wait(5)
        else:
            dispatch.join(5)
        with ad._records_lock:
            did = next(iter(ad._records))
        stopped = []
        stopper = threading.Thread(target=lambda: stopped.append(ad.interrupt_all("shutdown")))
        stopper.start()
        # Synchronize on the cancel request, not on a sleep or assumed thread speed.
        if during_insert:
            requested = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with ad._records_lock:
                    requested = ad._records[did].get("_cancel_requested")
                if requested:
                    break
                threading.Event().wait(0.01)
            assert requested
        release.set()
        stopper.join(5)
        dispatch.join(5)
        assert not stopper.is_alive() and not dispatch.is_alive()
        assert not started.is_set()
        assert _row(did)["state"] == "interrupted"
        assert dr.retry_status(did)["retry_state"] == "scheduled"
    finally:
        release.set()
        dispatch.join(5)
        ad._reset_for_tests()
