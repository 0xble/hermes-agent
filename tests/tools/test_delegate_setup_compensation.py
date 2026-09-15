"""Pre-child setup failures compensate only the grants this launch still owns."""
import asyncio
import json
import sqlite3
from unittest.mock import Mock

import pytest

from hermes_state import SessionDB
from tests.tools.test_delegate_launch_authority import parent, local_dispatch
from tools import async_delegation, delegate_tool
from tools.custom_subagents import ResolvedSubagentLaunch
from tools.registry import registry


@pytest.mark.parametrize("failure", ["history", "cancel", "validation", "storage", "successor", "lease"])
def test_setup_failure_restores_exact_sibling_resume_batch(
    tmp_path, monkeypatch, parent, local_dispatch, failure,
):
    db = SessionDB(tmp_path / "resume.db")
    parent._session_db = db
    ids = ["child-one", "child-two"]
    labels = ["Continue first", "Continue second"]
    owner = async_delegation.current_delegation_owner(parent)
    metadata = async_delegation.reserve_delegation_metadata(
        parent_task_id=None, owner=owner, task_labels=labels,
    )
    credentials = delegate_tool._resolve_delegation_credentials({}, parent)
    launches = {}
    for sid, label, ref in zip(ids, labels, metadata["thread_refs"]):
        db.create_session(sid, source="tool", model_config={"_delegation_completed": True})
        launches[sid] = ResolvedSubagentLaunch(
            None, credentials, None, resume_session_id=sid,
            launch_metadata={"card_identity": {
                "parent_task_id": metadata["parent_task_id"], "thread_ref": ref,
                "task_label": label, "owner": owner, "attempt": 0,
            }},
        )
    # Route authorization is unrelated to this failure interval. Keep preflight,
    # its atomic SessionDB batch claim, reservation and compensation real.
    monkeypatch.setattr(delegate_tool, "_resolve_resume_launch",
                        lambda task, *a, **k: launches[task["resume_session_id"]])
    build = Mock(side_effect=AssertionError("child construction must not begin"))
    monkeypatch.setattr(delegate_tool, "_build_children", build)
    captured = []

    def at_failure_boundary():
        configs = [json.loads(db.get_session(sid)["model_config"]) for sid in ids]
        claims = {config["_delegation_resume_claimed_at"] for config in configs}
        assert len(claims) == 1 and all(config["_delegation_completed"] is False for config in configs)
        claim = claims.pop()
        captured.append(claim)
        if failure == "successor":
            assert db.release_delegated_resumes(ids, claim_id=claim)
            assert db.claim_delegated_resumes(ids, claim_id="successor")
        if failure == "lease":
            assert db.acquire_session_turn_lease(ids[1], "live-owner", ttl_seconds=60)

    exception = {"history": RuntimeError, "cancel": KeyboardInterrupt, "validation": ValueError}.get(
        failure, sqlite3.OperationalError,
    )
    if failure in {"history", "cancel", "validation"}:
        def fail_history(*args, **kwargs):
            at_failure_boundary()
            raise exception("history setup rejected")
        monkeypatch.setattr("tools.delegation_history.prepare_task_histories", fail_history)
    else:
        def readonly_metadata_connection():
            at_failure_boundary()
            conn = sqlite3.connect(async_delegation._db_path())
            conn.execute("PRAGMA query_only=ON")
            return conn
        # The real reservation's BEGIN IMMEDIATE fails on a real SQLite connection.
        monkeypatch.setattr(async_delegation, "_connect", readonly_metadata_connection)
    tasks = [{"goal": "Continue the saved inspection", "task_label": label, "resume_session_id": sid}
             for sid, label in zip(ids, labels)]
    try:
        handler = registry.get_entry("delegate_task").handler
        if failure == "validation":
            assert "history setup rejected" in json.loads(handler({"tasks": tasks}, parent_agent=parent))["error"]
        else:
            with pytest.raises(exception):
                handler({"tasks": tasks}, parent_agent=parent)
        assert len(captured) == 1
        build.assert_not_called()
        assert local_dispatch == []
        configs = [json.loads(db.get_session(sid)["model_config"]) for sid in ids]
        if failure in {"successor", "lease"}:
            expected = "successor" if failure == "successor" else captured[0]
            assert all(config["_delegation_resume_claimed_at"] == expected for config in configs)
            assert all(config["_delegation_completed"] is False for config in configs)
            assert not db.claim_delegated_resumes(ids, claim_id="unsafe-retry")
        else:
            assert all(config["_delegation_completed"] is True for config in configs)
            assert all("_delegation_resume_claimed_at" not in config for config in configs)
            assert db.claim_delegated_resumes(ids, claim_id="retry")
            assert not db.release_delegated_resumes(ids, claim_id=captured[0])
    finally:
        db.release_session_turn_lease(ids[1], "live-owner")
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_replacement_commit_then_setup_error_keeps_primary_and_cancels_exact_claim(
    tmp_path, monkeypatch, parent, local_dispatch, cleanup_fails,
):
    from tests.gateway.test_delegation_handling import setup, drain
    from gateway.delegation_cards import DelegationCards

    cards, source, _, data, card_parent = await setup(tmp_path, monkeypatch)
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "failed"})
    await drain(cards)
    parent.session_id = card_parent.session_id
    parent.tool_progress_callback = card_parent.tool_progress_callback
    callback = parent.tool_progress_callback
    primary = sqlite3.OperationalError("replacement reply lost after commit")
    claimed = []

    def callback_with_lost_reply(event, **kwargs):
        result = callback(event, **kwargs)
        if kwargs.get("reason") == "validate_replacement":
            claimed.append(result["claim_id"])
            raise primary
        return result

    parent.tool_progress_callback = callback_with_lost_reply
    if cleanup_fails:
        monkeypatch.setattr(delegate_tool, "_release_resume_launches",
                            Mock(side_effect=sqlite3.OperationalError("resume cleanup unavailable")))
    build = Mock(side_effect=AssertionError("child construction must not begin"))
    monkeypatch.setattr(delegate_tool, "_build_children", build)
    task = {"goal": "Replace the failed inspection", "task_label": "Retry inspection",
            "replaces": {"parent_task_id": data["parent_task_id"], "thread_ref": "A"}}
    handler = registry.get_entry("delegate_task").handler
    with pytest.raises(sqlite3.OperationalError) as caught:
        await asyncio.to_thread(handler, {"tasks": [task]}, parent_agent=parent)
    assert caught.value is primary
    build.assert_not_called()
    assert local_dispatch == []
    assert len(claimed) == 1
    restored = DelegationCards(cards.runner, home=tmp_path, interval=0)
    card = restored.cards[data["parent_task_id"]]
    assert "A" not in card.get("replacement_claims", {})
    assert claimed[0] in card["replacement_cancellations"]["A"]
    with pytest.raises(ValueError, match="cancelled"):
        await restored.handling(source, "r", "s", 2, actor_session_id="s",
                                parent_task_id=data["parent_task_id"], refs=["A"],
                                reason="validate_replacement", detail=claimed[0])
