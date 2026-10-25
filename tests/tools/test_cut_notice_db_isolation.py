"""Cut-notice rendering is pure: it never reads state.db (PR #561 review P1/P2)."""

import json
import queue
import sqlite3
import time

import pytest

from tools import async_delegation as ad
from tools import delegation_resume as dr
from tools.process_registry import process_registry
from tools.process_registry_notifications import _format_async_delegation, format_process_notification

from tests.tools.test_delegation_recovery_admission import _cut_live_batch


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _locked(*_args, **_kwargs):
    raise sqlite3.OperationalError("database is locked")


def _recorded_cut(did="deleg_locked_cut", cut_reason="gateway shutdown (restart)"):
    evt = {"type": "async_delegation", "delegation_id": did, "goal": "only task", "status": "interrupted",
           "cut_reason": cut_reason, "completed_at": time.time(), "parent_session_id": "owner"}
    ad._persist_dispatch({**evt, "session_key": "", "dispatched_at": time.time(), "role": "leaf", "model": "m"})
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET state='interrupted', event_json=?, result_json=? "
                     "WHERE delegation_id=?", (json.dumps(evt), json.dumps(evt), did))
    return evt


def test_locked_ledger_does_not_lose_unrelated_completions(monkeypatch):
    cut = _recorded_cut()
    cut["resume_available"] = True  # stamped off-loop when the event was built
    plain = {"type": "completion", "session_id": "proc_unrelated", "command": "make", "exit_code": 0,
             "output": "built"}
    monkeypatch.setattr(process_registry, "_completions_restored", True)
    process_registry.completion_queue.put(plain)
    process_registry.completion_queue.put(cut)
    monkeypatch.setattr(ad, "_transaction", _locked)

    drained = process_registry.drain_notifications()

    assert [evt.get("session_id") or evt.get("delegation_id") for evt, _ in drained] == [
        "proc_unrelated", "deleg_locked_cut"]
    assert "action='resume'" in drained[1][1]


@pytest.mark.parametrize("stamped", [True, False, None])
def test_formatter_reads_only_the_event_stamp(monkeypatch, stamped):
    evt = {"type": "async_delegation", "delegation_id": "deleg_stamp", "goal": "only task",
           "status": "interrupted", "cut_reason": "gateway shutdown (restart)", "completed_at": time.time()}
    if stamped is not None:
        evt["resume_available"] = stamped
    monkeypatch.setattr(ad, "_transaction", _locked)
    text = format_process_notification(evt)
    assert ("action='resume'" in text) is bool(stamped)


def test_stamp_fails_closed_when_ledger_is_locked(monkeypatch):
    cut = _recorded_cut("deleg_stamp_locked")
    monkeypatch.setattr(ad, "_transaction", _locked)
    dr.stamp_resume_availability(cut)
    assert cut["resume_available"] is False
    assert "action='resume'" not in _format_async_delegation(cut)


def test_restored_cut_is_stamped_at_replay():
    cut = _recorded_cut("deleg_replayed_cut")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET delivery_state='pending' WHERE delegation_id=?",
                     (cut["delegation_id"],))
    q = queue.Queue()
    assert ad.restore_undelivered_completions(q) == 1
    restored = q.get_nowait()
    assert restored["resume_available"] is True
    assert "action='resume'" in _format_async_delegation(restored)


@pytest.mark.parametrize("cut_reason", ["gateway shutdown (post-interrupt)", "gateway shutdown (restart)"])
def test_cut_notice_with_retry_note_gives_exactly_one_instruction(cut_reason):
    did = "deleg_single_instruction"
    evt = _recorded_cut(did, cut_reason)
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET origin_session=?, retry_state='scheduled', retry_due_at=?, "
                     "retry_reason='interrupted' WHERE delegation_id=?",
                     ("agent:main:telegram:dm:1", time.time() + 300, did))
    record = dr.retry_status(did)
    evt["retry_note"] = dr.retry_note({"retry_state": record["retry_state"], "reason": "interrupted",
                                       "due_at": record["retry_due_at"], "attempt": 1,
                                       "delegation_id": did, "cut_reason": cut_reason})
    dr.stamp_resume_availability(evt)
    assert evt["resume_available"] is True
    text = _format_async_delegation(evt)
    resume_lines = [line for line in text.splitlines() if "action='resume'" in line]
    auto = "retry this automatically" in text
    assert len(resume_lines) + int(auto) == 1, text
    if cut_reason == "gateway shutdown (restart)":
        assert resume_lines and resume_lines[0].startswith("RETRY:")
    else:
        assert auto and "Do not re-dispatch" in text


def _claimed_legacy_recovery(did, session):
    claimed = _cut_live_batch(did, tui=True)
    assert claimed["status"] == "recovery_claimed"
    if session != "owner":
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET origin_ui_session_id='tui-other', "
                         "parent_session_id='sess-other' WHERE delegation_id=?", (did,))
    record = dr.retry_status(did)
    assert record["retry_state"] == dr.RETRY_NONE and record["resume_state"] == dr.RESUME_STATE_CLAIMED


def test_cli_stop_revokes_only_its_own_claimed_legacy_recovery():
    _claimed_legacy_recovery("deleg_legacy_mine", "owner")
    _claimed_legacy_recovery("deleg_legacy_theirs", "other")

    # An unscoped stop never revokes another session's claimed legacy recovery.
    ad.interrupt_all(reason="/stop")
    assert dr.retry_status("deleg_legacy_mine")["retry_state"] == dr.RETRY_NONE
    assert dr.retry_status("deleg_legacy_theirs")["retry_state"] == dr.RETRY_NONE

    ad.interrupt_all(reason="/stop", parent_session_id="sess-owner")
    assert dr.retry_status("deleg_legacy_mine")["retry_state"] == dr.RETRY_CANCELLED
    assert dr.retry_status("deleg_legacy_theirs")["retry_state"] == dr.RETRY_NONE
    assert dr.retry_status("deleg_legacy_theirs")["resume_state"] == dr.RESUME_STATE_CLAIMED


def test_cli_stop_command_scopes_durable_cancellation_to_its_session(monkeypatch):
    from hermes_cli.cli_commands_session_tools import CLICommandsSessionToolsMixin

    calls = []
    monkeypatch.setattr(process_registry, "list_sessions", lambda: [])
    monkeypatch.setattr(ad, "active_count", lambda: 1)
    monkeypatch.setattr(ad, "interrupt_all", lambda **kwargs: calls.append(kwargs) or 1)
    cli = type("CLI", (CLICommandsSessionToolsMixin,), {"session_id": "sess-cli"})()
    cli._handle_stop_command()
    assert calls == [{"reason": "/stop", "parent_session_id": "sess-cli"}]
