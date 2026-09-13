"""A rejected resume cannot replace its predecessor's durable checkpoint."""
import json
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tools import delegate_tool
from tools import delegate_tool_child_run as child_run
from tools.delegate_tool_checkpoint import prepare_resume_recovery


def _config(db):
    return json.loads(db.get_session("child")["model_config"])


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "resume.db")
    db.create_session("child", source="tool", model_config={
        "_delegation_completed": True, "_delegation_outcome": "budget_exhausted",
        "_delegation_launch": {"parent_session_root": "parent", "route": "unchanged"},
        "sentinel": "preserve",
    })
    db.append_message("child", "user", "Inspect source")
    db.append_message("child", "assistant", "Settled checkpoint")
    child = SimpleNamespace(
        _session_db=db, session_id="child", _delegation_resume_claim_id="exact-claim",
        _delegation_resume_admitted=False, _delegation_named_type="owner",
        tool_progress_callback=None, get_activity_summary=lambda: {"api_call_count": 1},
        close=lambda: None,
    )
    # Keep orchestration, await/result classification and checkpoint persistence real;
    # replace only the worker execution and unrelated launch/teardown lifecycle.
    monkeypatch.setattr(delegate_tool, "_lease_child_credential", lambda child: (None, None))
    monkeypatch.setattr(delegate_tool, "_start_heartbeat", lambda *a: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(delegate_tool, "_register_child", lambda *a, **k: None)
    monkeypatch.setattr(child_run._ChildRun, "seed_workspace", lambda run: setattr(run, "child_task_id", "offline-child"))
    monkeypatch.setattr(child_run._ChildRun, "cleanup", lambda *a, **k: None)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)
    yield db, child
    db.close()


def _claim(db, kind):
    if kind.startswith("recovery"):
        patch = {"_delegation_completed": False, "_delegation_outcome": "interrupted"}
        if kind == "recovery-stopped":
            patch.update(_delegation_user_stopped=True,
                         _delegation_resume_blocked_reason="user_stopped_requires_explicit_authorization")
        db.patch_session_model_config("child", patch)
        recovery = prepare_resume_recovery(
            {"resume_authorization": {"authorization": "Renewed parent request",
                                      "reconciliation": "Verified settled transcript"}},
            db, "child", _config(db), "parent",
        )
    else:
        recovery = None
    original = _config(db)
    assert db.claim_delegated_resumes(
        ["child"], claim_id="exact-claim", reconciliations={"child": recovery} if recovery else None,
    )
    return original


def _assert_restored(db, original):
    restored = _config(db)
    # Recovery's audit receipt survives compensation; all prior fields/absence
    # (including stop/blocker and route metadata) must otherwise be exact.
    audit = restored.pop("_delegation_resume_authorizations", None)
    if original["_delegation_outcome"] == "interrupted":
        assert audit[-1]["claim_id"] == "exact-claim"
    assert restored == original


@pytest.mark.parametrize("kind", ["ordinary", "recovery-stopped", "recovery-absent"])
@pytest.mark.parametrize("execution", ["exception", "structured-unsafe", "structured-safe"])
@pytest.mark.parametrize("admission", ["unadmitted", "admitted", "new", "other-claim", "busy-lease"])
def test_only_admitted_resume_can_publish_checkpoint(checkpoint, kind, execution, admission):
    db, child = checkpoint
    original = _claim(db, kind)
    if admission == "new":
        child._delegation_resume_claim_id = None
    child._delegation_resume_admitted = admission == "admitted"
    if admission == "other-claim":
        db.patch_session_model_config("child", {"_delegation_resume_claimed_at": "newer-claim"})
    if admission == "busy-lease":
        assert db.acquire_session_turn_lease("child", "competing-holder", ttl_seconds=60, wait_seconds=0)
    claimed = _config(db)
    messages = db.get_messages("child")

    def execute(**kwargs):
        if execution == "exception":
            raise RuntimeError("Native admission failed before execution")
        return {"completed": False, "interrupted": True, "api_calls": 0,
                "messages": db.get_messages_as_conversation("child", repair_alternation=False)
                if execution == "structured-safe" else [], "final_response": "Admission rejected"}

    child.run_conversation = execute
    entry = delegate_tool._run_single_child(0, "Continue", child=child)
    assert entry["status"] == ("error" if execution == "exception" else "interrupted")
    if admission == "unadmitted":
        _assert_restored(db, original)
    elif admission in {"other-claim", "busy-lease"}:
        assert _config(db) == claimed
    elif execution == "structured-safe":
        assert _config(db)["_delegation_completed"] is True
        assert entry["resume_available"] is True
    else:
        assert _config(db)["_delegation_completed"] is False
        assert _config(db)["_delegation_resume_blocked_reason"] == "unresolved_tool_effects_or_uncheckpointed_history"
        assert entry["resume_available"] is False
    assert db.get_messages("child") == messages


@pytest.mark.parametrize("kind", ["ordinary", "recovery-stopped"])
@pytest.mark.parametrize("late_admitted", [False, True])
def test_deferred_worker_retains_claim_until_settled(checkpoint, monkeypatch, kind, late_admitted):
    db, child = checkpoint
    original = _claim(db, kind)
    claimed = _config(db)
    future = Future()
    # Deterministic pending worker, no sleep/model: exercise real timeout handling
    # and its Future completion callback with admission still possible afterward.
    executor = SimpleNamespace(submit=lambda *a, **k: future, shutdown=lambda **k: None)
    monkeypatch.setattr("tools.daemon_pool.DaemonThreadPoolExecutor", lambda **k: executor)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0)
    closed = []
    child.close = lambda: closed.append(_config(db))
    entry = delegate_tool._run_single_child(0, "Continue", child=child)
    assert entry["status"] == "timeout"
    assert _config(db) == claimed
    assert not closed
    if late_admitted:
        assert db.acquire_session_turn_lease("child", "late-worker", ttl_seconds=60, wait_seconds=0)
        child._delegation_resume_admitted = True
        db.release_session_turn_lease("child", "late-worker")
    future.set_result({"completed": False})
    assert closed
    if late_admitted:
        assert _config(db) == claimed
    else:
        _assert_restored(db, original)
