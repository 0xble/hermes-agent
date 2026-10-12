"""Exercise actual durable cut, notice acknowledgement, and replacement admission."""

import json
import time

import pytest

from tools import async_delegation as ad
from tools import delegation_resume as dr
from tools.delegate_tool_registry import _handle_control_action


class Parent:
    session_id = "owner"
    _session_db = None


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")


def cut_row(did="deleg_restart_flow", *, batch=True):
    now = time.time()
    record = dict(delegation_id=did, goal="first; second; third", context="shared",
                  role="leaf", model="m", session_key="agent:main:telegram:dm:1",
                  parent_session_id="owner", origin_session_id="owner", retry_state="armed",
                  dispatched_at=now - 60)
    if batch:
        record.update(is_batch=True, goals=["first", "second", "third"], task_indexes=[0, 1, 2])
    ad._persist_dispatch(record)
    event = dict(type="async_delegation", delegation_id=did, status="interrupted",
                 cut_reason="gateway shutdown (restart)", completed_at=now)
    result = {"status": "interrupted"}
    if batch:
        result.update(results=[{"task_index": 0, "status": "completed", "summary": "done"},
                               {"task_index": 1, "status": "interrupted"}], partial=True)
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET state='interrupted', result_json=?, event_json=?, "
                     "completed_at=?, updated_at=? WHERE delegation_id=?",
                     (json.dumps(result), json.dumps(event), now, now, did))
    return did, event, now


def test_cut_batch_is_classified_by_existing_retry_engine():
    did, _, now = cut_row()
    plan = dr.schedule_retry(did, now=now)
    assert plan["retry_state"] == "scheduled"
    payload = json.loads(_handle_control_action("resume", did, None, Parent()))
    assert payload["status"] == "retry_claimed"
    assert [task["goal"] for task in payload["tasks"]] == ["second", "third"]


@pytest.mark.parametrize("batch", [False, True])
def test_delivered_cut_notice_does_not_create_second_parent_notice(batch):
    did, event, now = cut_row(batch=batch)
    dr.schedule_retry(did, now=now)
    claim = ad.claim_event_delivery(event, "test")
    assert claim is not None
    assert ad.complete_completion_delivery(did, claim)
    assert dr.sweep_retries(now=now + 301) == []
    assert dr.sweep_retries(now=now + dr.NOTICE_GRACE_S + 301) == []
    assert dr.sweep_retries(now=now + dr.NOTICE_GRACE_S * 3) == []
    payload = json.loads(_handle_control_action("resume", did, None, Parent()))
    assert payload["status"] == "retry_claimed"


def test_each_claimed_child_admits_one_replacement_without_rerunning_success():
    did, _, now = cut_row()
    dr.schedule_retry(did, now=now)
    payload = json.loads(_handle_control_action("resume", did, None, Parent()))
    assert payload["status"] == "retry_claimed"
    for index, task in enumerate(payload["tasks"]):
        record = dict(delegation_id=f"deleg_replacement_{index}", goal=task["goal"],
                      context=task["context"], session_key="telegram:dm:1",
                      parent_session_id="owner", origin_session_id="owner", dispatched_at=now)
        ad._persist_dispatch(record)
        with pytest.raises(dr.RetryAdmissionError, match="consumed"):
            ad._persist_dispatch(dict(record, delegation_id=f"deleg_duplicate_{index}"))
    with ad._DB_LOCK, ad._transaction() as conn:
        replacements = conn.execute("SELECT task_index, replacement_delegation_id FROM "
                                    "async_delegation_child_recovery WHERE delegation_id=? ORDER BY task_index",
                                    (did,)).fetchall()
    assert replacements == [(1, "deleg_replacement_0"), (2, "deleg_replacement_1")]


def test_unclaimed_child_marker_does_not_authorize_replacement():
    did, _, _ = cut_row()
    with pytest.raises(dr.RetryAdmissionError, match="claim"):
        ad._persist_dispatch(dict(delegation_id="deleg_forged", goal="first",
                                  context=f"RECOVERY OF INTERRUPTED DELEGATION {did}.\nRECOVERY CHILD INDEX: 0",
                                  session_key="telegram:dm:1", parent_session_id="owner",
                                  origin_session_id="owner", dispatched_at=time.time()))


@pytest.mark.parametrize("batch", [False, True])
def test_unresumed_cut_has_explicit_contract_then_expires_and_prunes(batch, monkeypatch):
    did, event, now = cut_row(batch=batch)
    ad._attach_retry_note(event)
    assert "action='resume'" in event["retry_note"]
    assert "retry automatically" not in event["retry_note"]
    claim = ad.claim_event_delivery(event, "test")
    assert ad.complete_completion_delivery(did, claim)
    assert dr.sweep_retries(now=now + 301) == []
    assert dr.sweep_retries(now=now + dr.MAX_LINEAGE_AGE_S + 1) == []
    expired = dr.retry_status(did)
    assert expired["retry_state"] == "reported"
    assert "24h" in expired["retry_reason"]
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET updated_at=? WHERE delegation_id=?",
                     (now - ad._DURABLE_RETENTION_SECONDS - 1, did))
    ad._prune_durable_records()
    assert dr.retry_status(did) is None


def test_unspent_cut_claim_rearms_quietly_for_explicit_resume():
    did, event, now = cut_row()
    dr.schedule_retry(did, now=now)
    delivery = ad.claim_event_delivery(event, "test")
    assert ad.complete_completion_delivery(did, delivery)
    first, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    assert dr.sweep_retries(now=first["retry_claimed_at"] + dr.NOTICE_GRACE_S + 1) == []
    again, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    assert [task["goal"] for task in again["recovery_tasks"]] == ["second", "third"]
