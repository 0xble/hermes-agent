"""One immutable owner across reservation, result retrieval and card disposition."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_state import SessionDB
from tools import async_delegation, delegate_tool
from gateway.config import Platform
from gateway.delegation_cards import DelegationCards
from gateway.platforms.base import SendResult
from gateway.session import SessionSource


@pytest.fixture
def owners(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="cli")
    db.end_session("root", "compression")
    db.create_session("tip", source="cli", parent_session_id="root")
    db.create_session("sibling", source="cli", parent_session_id="root")
    db.create_session("foreign", source="cli")
    original = dict(profile=str(tmp_path), session_id="root", session_key="", chat_id="", thread_id="", topic_id="")
    yield db, original, {**original, "session_id": "tip"}
    db.close()


@pytest.mark.parametrize("mutation", [None, "foreign", "sibling", "noncompression", "profile", "session_key", "chat_id", "thread_id", "topic_id"])
def test_original_ledger_owner_survives_only_proven_compression(owners, mutation):
    db, original, current = owners
    metadata = async_delegation.reserve_delegation_metadata(parent_task_id=None, owner=original, task_labels=["Inspect"])
    result = {"results": [{"task_index": 0, "status": "completed", "summary": "Fixture result"}]}
    delegation_id = async_delegation.persist_inline_result(result, metadata)
    if mutation in ("foreign", "sibling"):
        current["session_id"] = mutation
    elif mutation == "noncompression":
        db.reopen_session("root")
        db.end_session("root", "reset")
    elif mutation:
        current[mutation] = "foreign-scope"
    if mutation:
        with pytest.raises(ValueError, match="immutable conversation owner"):
            async_delegation.reserve_delegation_metadata(parent_task_id=metadata["parent_task_id"],
                owner=current, task_labels=["Inspect"], resume_refs=["A"], session_db=db)
        assert async_delegation.get_delegation_result(delegation_id, owner=current, session_db=db) is None
        assert async_delegation.get_delegation_status(delegation_id, owner=current, session_db=db) is None
        assert async_delegation.list_durable_delegations(owner=current, session_db=db) == []
    else:
        restored = async_delegation.reserve_delegation_metadata(parent_task_id=metadata["parent_task_id"],
            owner=current, task_labels=["Inspect"], resume_refs=["A"], session_db=db)
        assert restored == metadata
        parent = SimpleNamespace(session_id="tip", _session_db=db)
        payload = json.loads(delegate_tool.delegate_task(action="result", delegation_id=delegation_id, parent_agent=parent))
        assert payload["results"] == result["results"]
        assert payload["owner"] == original
        assert async_delegation.get_delegation_status(delegation_id, owner=current, session_db=db)
        assert len(async_delegation.list_durable_delegations(owner=current, session_db=db)) == 1
    stored = async_delegation.get_delegation_result(delegation_id, owner=original)
    assert stored is not None
    assert stored["delegation_metadata"]["owner"] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "foreign", "sibling", "noncompression", "profile", "session_key", "chat_id", "thread_id", "topic_id"])
async def test_card_admission_result_handling_preserve_original_owner(owners, tmp_path, mutation):
    import asyncio
    db, original, current = owners
    original.update(chat_id="42", session_key="fixture")
    current.update(chat_id="42", session_key="fixture")
    adapter = SimpleNamespace(
        send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        edit_message=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter,
        session_store=SimpleNamespace(_db_for_key=lambda key: db if key == "fixture" else None))
    cards = DelegationCards(runner, home=tmp_path, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    metadata = async_delegation.reserve_delegation_metadata(parent_task_id=None, owner=original, task_labels=["Inspect"])
    key = metadata["parent_task_id"]
    data = dict(parent_task_id=key, thread_ref="A", task_label="Inspect", owner=original, card_owner=original,
                child_session_id="child", attempt=0)
    await cards.observe(source, "fixture", "root", 1, "subagent.start", None, data)
    await cards.observe(source, "fixture", "root", 1, "subagent.complete", None, {**data, "status": "completed"})
    await asyncio.gather(*list(cards.pending.values()))
    before = deepcopy(cards.cards[key])
    if mutation in ("foreign", "sibling"):
        current["session_id"] = mutation
    elif mutation == "noncompression":
        db.reopen_session("root")
        db.end_session("root", "reset")
    elif mutation:
        current[mutation] = "foreign-scope"
    actor = current["session_id"]
    # Full owner scope is required when the actor differs from the original.
    presented = [{"parent_task_id": key, "thread_refs": ["A"], "attempts": {"A": 0}}]
    await cards.result_turn(actor_session_id=actor, actor_owner=current, turn_id="turn", results=presented)
    if mutation:
        assert "turn" not in cards.cards[key].get("result_turns", {})
        with pytest.raises(ValueError, match="exact parent owner"):
            await cards.handling(source, "fixture", actor, 2, actor_session_id=actor, actor_owner=current,
                                 parent_task_id=key, refs=["A"], reason="incorporated", turn_id="turn")
        if mutation in ("foreign", "sibling", "noncompression"):
            await cards.observe(source, "fixture", actor, 2, "subagent.admitted", None,
                                {**data, "attempt": 1, "resume_claim_id": "claim"})
            assert cards.cards[key] == before
    else:
        answer = await cards.handling(source, "fixture", actor, 2, actor_session_id=actor, actor_owner=current,
                                      parent_task_id=key, refs=["A"], reason="incorporated", turn_id="turn")
        assert answer["awaiting_delivery"]
        card = cards.cards[key]
        assert card["handling"]["A"]["actor_session_id"] == "root"
        assert not card.get("handled")  # attestation is not delivery
        await cards.observe(source, "fixture", actor, 2, "subagent.admitted", None,
                            {**data, "attempt": 1, "resume_claim_id": "claim"})
        await asyncio.gather(*list(cards.pending.values()))
        assert card["owner"] == card["delegation_owner"] == original
        assert card["rows"]["A"]["attempt"] == 1
        assert card["rows"]["A"]["state"] == "running"
        assert card["attempt_history"]["A"]["0"]["disposition"]["actor_session_id"] == "root"
        with pytest.raises(ValueError, match="absent or superseded"):
            # Old presentation cannot acknowledge a later terminal attempt.
            await cards.observe(source, "fixture", actor, 2, "subagent.complete", None,
                                {**data, "attempt": 1, "status": "completed"})
            await cards.handling(source, "fixture", actor, 2, actor_session_id=actor, actor_owner=current,
                                 parent_task_id=key, refs=["A"], reason="incorporated", turn_id="turn")
    await asyncio.gather(*list(cards.pending.values()))
