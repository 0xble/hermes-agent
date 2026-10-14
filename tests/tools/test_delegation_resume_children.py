"""Per-child recovery claims for interrupted background fan-outs."""

from __future__ import annotations

import json
import time

import pytest

from tools import async_delegation as ad
from tools import delegation_resume as dr
from tools.delegate_tool_registry import _handle_control_action


class _Parent:
    session_id = "sess-owner"
    _session_db = None


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    yield


def _batch_row(did="deleg_batch_resume"):
    record = {
        "delegation_id": did,
        "goal": "3 parallel subagents: first; second; third",
        "goals": ["first", "second", "third"],
        "is_batch": True,
        "task_indexes": [0, 1, 2],
        "task_transcripts": {"1": "/tmp/second.txt"},
        "context": "shared context",
        "toolsets": None,
        "role": "leaf",
        "model": "m",
        "session_key": "telegram:dm:1",
        "origin_ui_session_id": "",
        "origin_session_id": "sess-owner",
        "parent_session_id": "sess-owner",
        "dispatched_at": time.time() - 60,
    }
    ad._persist_dispatch(record)
    now = time.time()
    result = {
        "results": [
            {"task_index": 0, "status": "completed", "summary": "already done"},
            {"task_index": 1, "status": "unknown", "error": "cut"},
        ],
        "partial": True,
    }
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET retry_state='none', state='interrupted', completed_at=?, "
            "updated_at=?, result_json=?, event_json=? WHERE delegation_id=?",
            (now, now, json.dumps(result), json.dumps({"status": "interrupted"}), did),
        )
    return did


def test_batch_resume_returns_one_shot_tasks_only_for_unfinished_children():
    did = _batch_row()
    raw = _handle_control_action("resume", did, None, _Parent())
    payload = json.loads(raw)
    assert payload["status"] == "recovery_claimed"
    assert [task["goal"] for task in payload["tasks"]] == ["second", "third"]
    assert all("verify current state" in task["context"] for task in payload["tasks"])
    with ad._DB_LOCK, ad._transaction() as conn:
        claimed = {
            row[0] for row in conn.execute(
                "SELECT task_index FROM async_delegation_child_recovery WHERE delegation_id=?", (did,)
            )
        }
    assert claimed == {1, 2}

    second = _handle_control_action("resume", did, None, _Parent())
    assert "already" in second.lower() or "one-shot" in second.lower()


def test_retry_owned_batch_claims_only_unfinished_children():
    did = _batch_row("deleg_retry_batch")
    now = time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET retry_state='scheduled', retry_reason='timeout', retry_due_at=?, "
            "retry_root=?, retry_root_started_at=? WHERE delegation_id=?",
            (now, did, now, did),
        )
    payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
    assert payload["status"] == "retry_claimed"
    assert [task["goal"] for task in payload["tasks"]] == ["second", "third"]
    second = _handle_control_action("resume", did, None, _Parent())
    assert "duplicate" in second.lower() or "not awaiting" in second.lower()


def test_batch_resume_preserves_recorded_child_evidence():
    did = _batch_row("deleg_batch_evidence")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET retry_state='none' WHERE delegation_id=?", (did,))
    payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
    assert "/tmp/second.txt" in payload["tasks"][0]["context"]


def _scheduled_batch(did="deleg_retry_release"):
    did = _batch_row(did)
    now = time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET retry_state='scheduled', retry_reason='timeout', "
                     "retry_due_at=?, retry_root=?, retry_root_started_at=?, delivery_state='delivered' "
                     "WHERE delegation_id=?", (now,did,now,did))
    return did


def _persist_child(did, task):
    ad._persist_dispatch(dict(delegation_id=did, goal=task["goal"], context=task["context"],
                             role="leaf", session_key="telegram:dm:1", parent_session_id="sess-owner",
                             origin_session_id="sess-owner", dispatched_at=time.time()))


def _scalar_row(did="deleg_scalar_resume"):
    now = time.time()
    record = {
        "delegation_id": did,
        "goal": "finish the scalar task",
        "context": "shared context",
        "toolsets": None,
        "role": "leaf",
        "model": "m",
        "session_key": "telegram:dm:1",
        "origin_session_id": "sess-owner",
        "parent_session_id": "sess-owner",
        "dispatched_at": now - 60,
    }
    ad._persist_dispatch(record)
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET retry_state='none', state='interrupted', completed_at=?, "
            "updated_at=?, result_json=?, event_json=? WHERE delegation_id=?",
            (now, now, json.dumps({"status": "interrupted"}),
             json.dumps({"status": "interrupted"}), did),
        )
    return did


@pytest.mark.parametrize("shape", ["scalar", "batch"])
def test_explicit_resume_claim_released_after_failed_persist_then_resume(shape, monkeypatch):
    """A replacement INSERT failure must not permanently spend an explicit resume claim."""
    if shape == "scalar":
        did = _scalar_row()
        payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
        goal = payload["goal"]
        context = payload["recovery_context"]
    else:
        did = _batch_row("deleg_batch_explicit_resume")
        payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
        assert [task["goal"] for task in payload["tasks"]] == ["second", "third"]
        # The first child is genuinely consumed; a later failed sibling persist must
        # preserve it while releasing only the still-unspent child claim.
        _persist_child("deleg_explicit_consumed", payload["tasks"][0])
        goal = payload["tasks"][1]["goal"]
        context = payload["tasks"][1]["context"]

    with monkeypatch.context() as m:
        m.setattr(ad, "_persist_dispatch", lambda record: (_ for _ in ()).throw(OSError("persist failed")))
        result = ad.dispatch_async_delegation(
            goal=goal, context=context, role="leaf", model="m", toolsets=None,
            session_key="telegram:dm:1", parent_session_id="sess-owner",
            runner=lambda: pytest.fail("failed recovery ran inline"),
            delegation_id=f"deleg_{shape}_explicit_failed", max_async_children=1,
        )

    assert result["status"] == "rejected"
    assert result["no_inline_fallback"] is True
    prior = dr.retry_status(did)
    assert prior["resume_state"] == dr.RESUME_STATE_NONE
    assert prior["resume_attempts"] == 0
    assert not prior["resume_claim"]

    retry = json.loads(_handle_control_action("resume", did, None, _Parent()))
    assert retry["status"] == "recovery_claimed"
    if shape == "scalar":
        assert retry["goal"] == goal
    else:
        assert [task["goal"] for task in retry["tasks"]] == ["third"]
        with ad._DB_LOCK, ad._transaction() as conn:
            consumed = conn.execute(
                "SELECT replacement_delegation_id FROM async_delegation_child_recovery "
                "WHERE delegation_id=? AND task_index=1", (did,)
            ).fetchone()
        assert consumed == ("deleg_explicit_consumed",)


def test_scalar_duplicate_consumed_brief_does_not_rearm_resume_claim():
    did = _scalar_row("deleg_scalar_consumed")
    payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
    ad._persist_dispatch(dict(
        delegation_id="deleg_scalar_consumed_replacement", goal=payload["goal"],
        context=payload["recovery_context"], role="leaf", session_key="telegram:dm:1",
        parent_session_id="sess-owner", origin_session_id="sess-owner", dispatched_at=time.time(),
    ))

    result = ad.dispatch_async_delegation(
        goal=payload["goal"], context=payload["recovery_context"], role="leaf", model="m", toolsets=None,
        session_key="telegram:dm:1", parent_session_id="sess-owner",
        runner=lambda: pytest.fail("consumed recovery ran inline"), max_async_children=1,
    )

    assert result["status"] == "rejected"
    assert result["no_inline_fallback"] is True
    assert "will be retried later" not in result["error"]
    prior = dr.retry_status(did)
    assert prior["resume_state"] == dr.RESUME_STATE_CLAIMED
    assert prior["resume_claim"]
    assert prior["retry_replacement"] == "deleg_scalar_consumed_replacement"
    assert did not in {row["delegation_id"] for row in dr.list_boot_candidates()}
    second = _handle_control_action("resume", did, None, _Parent())
    assert "already" in second.lower() or "one-shot" in second.lower()


def test_batch_duplicate_consumed_child_preserves_dispatch_and_sibling_claim():
    did = _scheduled_batch("deleg_batch_consumed_duplicate")
    payload, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    child0, child1 = payload["recovery_tasks"]
    _persist_child("deleg_batch_child_zero", child0)

    duplicate = ad.dispatch_async_delegation(
        goal=child0["goal"], context=child0["context"], role="leaf", model="m", toolsets=None,
        session_key="telegram:dm:1", parent_session_id="sess-owner",
        runner=lambda: pytest.fail("consumed recovery ran inline"), max_async_children=1,
    )

    assert duplicate["status"] == "rejected"
    assert duplicate["no_inline_fallback"] is True
    prior = dr.retry_status(did)
    assert prior["retry_state"] == dr.RETRY_DISPATCHING
    with ad._DB_LOCK, ad._transaction() as conn:
        sibling = conn.execute(
            "SELECT replacement_delegation_id FROM async_delegation_child_recovery "
            "WHERE delegation_id=? AND task_index=2", (did,)
        ).fetchone()
    assert sibling == (None,)

    _persist_child("deleg_batch_child_one", child1)
    assert dr.retry_status(did)["retry_state"] == dr.RETRY_DISPATCHED


def test_batch_failed_persist_releases_child_claims_then_resume(monkeypatch):
    did = _scheduled_batch()
    payload = json.loads(_handle_control_action("resume", did, None, _Parent()))
    assert payload["status"] == "retry_claimed"
    persist = ad._persist_dispatch
    ran = []
    with monkeypatch.context() as m:
        m.setattr(ad, "_persist_dispatch", lambda record: (_ for _ in ()).throw(OSError("persist failed")))
        task = payload["tasks"][0]
        result = ad.dispatch_async_delegation(
            goal=task["goal"], context=task["context"], role="leaf", model="m", toolsets=None,
            session_key="telegram:dm:1", parent_session_id="sess-owner",
            runner=lambda: ran.append(True), max_async_children=1,
        )
    assert result["status"] == "rejected"
    assert result["no_inline_fallback"] is True
    assert not ran
    assert ad._persist_dispatch == persist
    retry = json.loads(_handle_control_action("resume", did, None, _Parent()))
    assert retry["status"] == "retry_claimed"
    assert [task["goal"] for task in retry["tasks"]] == ["second", "third"]
    for i, task in enumerate(retry["tasks"]):
        _persist_child(f"deleg_released_child_{i}", task)
    assert dr.retry_status(did)["retry_state"] == "dispatched"


def test_unspawned_batch_claim_rearms_after_grace_notice():
    did = _scheduled_batch("deleg_grace_batch")
    first, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    now = first["retry_claimed_at"] + dr.NOTICE_GRACE_S + 1
    actions = dr.sweep_retries(now=now)
    assert len(actions) == 1 and actions[0]["kind"] == "notice"
    assert dr.mark_notice_accepted(did, actions[0]["claim"], now=now)
    again, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    assert [task["goal"] for task in again["recovery_tasks"]] == ["second", "third"]


def test_release_preserves_consumed_child_and_stale_release_cannot_clear_new_claim():
    did = _scheduled_batch("deleg_partial_release")
    first, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    _persist_child("deleg_consumed_second", first["recovery_tasks"][0])
    assert dr.release_retry_dispatch(first)
    again, reason = dr.claim_retry_dispatch(did)
    assert reason is None
    assert [task["goal"] for task in again["recovery_tasks"]] == ["third"]
    assert not dr.release_retry_dispatch(first)
    _persist_child("deleg_only_third", again["recovery_tasks"][0])
    assert dr.retry_status(did)["retry_state"] == "dispatched"
    with pytest.raises(dr.RetryAdmissionError, match="consumed|superseded"):
        _persist_child("deleg_duplicate_second", first["recovery_tasks"][0])


def test_retry_notice_tells_fanout_parent_to_use_tasks_list():
    did = _scheduled_batch("deleg_notice_tasks")
    assert "delegate_task(tasks=<returned tasks list>)" in dr.build_retry_notice(dr.retry_status(did))
