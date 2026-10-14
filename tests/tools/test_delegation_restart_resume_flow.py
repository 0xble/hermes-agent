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


def cut_row(did="deleg_restart_flow", *, batch=True, task_contexts=None):
    now = time.time()
    record = dict(delegation_id=did, goal="first; second; third", context="shared",
                  role="leaf", model="m", session_key="agent:main:telegram:dm:1",
                  parent_session_id="owner", origin_session_id="owner", retry_state="armed",
                  dispatched_at=now - 60)
    if batch:
        record.update(is_batch=True, goals=["first", "second", "third"], task_indexes=[0, 1, 2])
        if task_contexts is not None:
            record["task_contexts"] = task_contexts
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


def test_each_claimed_child_recovers_only_its_own_context():
    did, _, now = cut_row(
        "deleg_contexts",
        task_contexts={"0": "context-first", "1": "context-second", "2": "context-third"},
    )
    dr.schedule_retry(did, now=now)
    payload = json.loads(_handle_control_action("resume", did, None, Parent()))
    assert payload["status"] == "retry_claimed"
    recovered = {task["goal"]: task["context"] for task in payload["tasks"]}
    assert "context-second" in recovered["second"]
    assert "context-first" not in recovered["second"]
    assert "context-third" not in recovered["second"]
    assert "context-third" in recovered["third"]
    assert "context-first" not in recovered["third"]
    assert "context-second" not in recovered["third"]


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


def test_second_cut_of_recovered_fanout_admits_only_current_generation():
    did, _, now = cut_row("deleg_gen0")
    dr.schedule_retry(did, now=now)
    first = json.loads(_handle_control_action("resume", did, None, Parent()))
    tasks = first["tasks"]
    ad._persist_dispatch(dict(
        delegation_id="deleg_gen1", goal="two parallel", goals=[task["goal"] for task in tasks],
        is_batch=True, context="\n\n".join(dict.fromkeys(task["context"] for task in tasks)),
        session_key="agent:main:telegram:dm:1", parent_session_id="owner", origin_session_id="owner",
        retry_state="armed", dispatched_at=now,
    ))
    assert dr.retry_status(did)["retry_state"] == "dispatched"
    event = dict(type="async_delegation", delegation_id="deleg_gen1", status="interrupted",
                 cut_reason="gateway shutdown (restart)", completed_at=now)
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET state='interrupted', result_json=?, event_json=?, "
                     "completed_at=?, updated_at=? WHERE delegation_id=?",
                     (json.dumps({"status": "interrupted", "results": [
                         {"task_index": 0, "status": "interrupted"}], "partial": True}),
                      json.dumps(event), now, now, "deleg_gen1"))
    assert dr.schedule_retry("deleg_gen1", now=now)["retry_state"] == "scheduled"
    second = json.loads(_handle_control_action("resume", "deleg_gen1", None, Parent()))
    assert second["status"] == "retry_claimed"
    assert [task["goal"] for task in second["tasks"]] == ["second", "third"]
    for index, task in enumerate(second["tasks"]):
        ad._persist_dispatch(dict(
            delegation_id=f"deleg_gen2_{index}", goal=task["goal"], context=task["context"],
            session_key="telegram:dm:1", parent_session_id="owner", origin_session_id="owner",
            dispatched_at=now,
        ))
        with ad._DB_LOCK, ad._transaction() as conn:
            receipts = conn.execute("SELECT task_index, replacement_delegation_id FROM "
                                    "async_delegation_child_recovery WHERE delegation_id=? ORDER BY task_index",
                                    ("deleg_gen1",)).fetchall()
        assert receipts == [(0, "deleg_gen2_0"), (1, "deleg_gen2_1" if index == 1 else None)]
    assert dr.retry_status("deleg_gen1")["retry_state"] == "dispatched"


def test_nested_ancestor_markers_do_not_consume_sibling_claim():
    did, _, now = cut_row("deleg_link_scope")
    task = dr.retry_status(did)["task"]
    task["context"] = "RECOVERY CHILD INDEX: deleg_ancestor:2\nRECOVERY CHILD INDEX: 2"
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET task_json=? WHERE delegation_id=?", (json.dumps(task), did))
    dr.schedule_retry(did, now=now)
    payload = json.loads(_handle_control_action("resume", did, None, Parent()))
    task = payload["tasks"][0]
    ad._persist_dispatch(dict(
        delegation_id="deleg_link_first", goal=task["goal"], context=task["context"],
        session_key="telegram:dm:1", parent_session_id="owner", origin_session_id="owner",
        dispatched_at=now,
    ))
    with ad._DB_LOCK, ad._transaction() as conn:
        receipts = conn.execute("SELECT task_index, replacement_delegation_id FROM "
                                "async_delegation_child_recovery WHERE delegation_id=? ORDER BY task_index",
                                (did,)).fetchall()
    assert receipts == [(1, "deleg_link_first"), (2, None)]
    assert dr.retry_status(did)["retry_state"] == "dispatching"


def test_unclaimed_child_marker_does_not_authorize_replacement():
    did, _, _ = cut_row()
    with pytest.raises(dr.RetryAdmissionError, match="claim"):
        ad._persist_dispatch(dict(delegation_id="deleg_forged", goal="first",
                                  context=f"RECOVERY OF INTERRUPTED DELEGATION {did}.\nRECOVERY CHILD INDEX: {did}:0",
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


def test_restart_runner_crash_resumes_only_children_unfinished_by_checkpoint(monkeypatch):
    """A checkpoint-completed child survives a shutdown-time aggregation crash and is never resumed."""
    import queue
    import threading

    from tools.process_registry import process_registry

    ad._reset_for_tests()
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    did, checkpointed, release = "deleg_restart_crash", threading.Event(), threading.Event()

    def runner():
        ad.record_unit_child(did, {"task_index": 0, "status": "completed", "summary": "durable first"})
        checkpointed.set()
        assert release.wait(5)
        raise RuntimeError("shutdown aggregation failed")

    handle = ad.dispatch_async_delegation_batch(
        goals=["first", "second", "third"], delegation_id=did, context="shared", toolsets=None,
        role="leaf", model="m", session_key="agent:main:telegram:dm:1", parent_session_id="owner",
        runner=runner, interrupt_fn=lambda reason=None: release.set(), max_async_children=1)
    assert handle["accepted"] and checkpointed.wait(5)
    assert ad.interrupt_all("gateway shutdown (restart)") == 1
    event = process_registry.completion_queue.get(timeout=5)
    deadline = time.monotonic() + 5
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert event["cut_reason"] == "gateway shutdown (restart)"
    assert [child["status"] for child in event["results"]] == ["completed", "unknown", "unknown"]
    claim = ad.claim_event_delivery(event, "test")
    assert claim and ad.complete_completion_delivery(did, claim)
    assert process_registry.completion_queue.empty()
    assert dr.sweep_retries(now=time.time() + dr.NOTICE_GRACE_S * 3) == []
    payload = json.loads(_handle_control_action("resume", did, None, Parent()))
    assert payload["status"] == "retry_claimed"
    assert [task["goal"] for task in payload["tasks"]] == ["second", "third"]
    with ad._DB_LOCK, ad._transaction() as conn:
        claimed = [row[0] for row in conn.execute(
            "SELECT task_index FROM async_delegation_child_recovery WHERE delegation_id=? ORDER BY task_index",
            (did,))]
    assert claimed == [1, 2]
    ad._reset_for_tests()


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
