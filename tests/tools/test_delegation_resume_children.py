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
