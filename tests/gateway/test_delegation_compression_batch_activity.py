"""Real DB compression authority composes with birth-call TTL and activity fences."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.delegation_cards import DelegationCards
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from tests.gateway.test_delegation_cards import drain_cards
from tests.tools.test_delegation_compression_owner import owners  # noqa: F401
from tools import async_delegation


@pytest.mark.asyncio
async def test_compressed_owner_resume_reopens_batch_with_fenced_activity(owners, tmp_path):
    db, original, current = owners
    original.update(chat_id="42", session_key="fixture")
    current.update(chat_id="42", session_key="fixture")
    adapter = SimpleNamespace(
        send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter,
        session_store=SimpleNamespace(_db_for_key=lambda key: db if key == "fixture" else None))
    now = [100.0]
    cards = DelegationCards(runner, home=tmp_path, interval=0, clock=lambda: now[0])
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    metadata = async_delegation.reserve_delegation_metadata(
        parent_task_id=None, owner=original, task_labels=["Inspect", "Verify"])
    key = metadata["parent_task_id"]
    manifest = next(iter(metadata["original_calls"].values()))
    data = dict(parent_task_id=key, thread_ref="A", task_label="Inspect", owner=original,
                card_owner=original, child_session_id="child-A", attempt=0, original_call=manifest)
    result = {"results": [{"task_index": 0, "status": "completed", "summary": "Retained result"}]}
    delegation_id = async_delegation.persist_inline_result(result, {**metadata,
        "threads": [{"thread_ref": "A", "task_index": 0, "task_label": "Inspect"}]})
    try:
        for ref in ("A", "B"):
            event = {**data, "thread_ref": ref, "child_session_id": "child-" + ref}
            await cards.observe(source, "fixture", "root", 1, "subagent.start", None, event)
            await cards.observe(source, "fixture", "root", 1, "subagent.complete", None,
                                {**event, "status": "completed", "activity_sequence": 1})
        await drain_cards(cards)
        card = cards.cards[key]
        assert card["original_calls"][manifest["id"]]["display_expires_at"] == 400
        resumed = async_delegation.reserve_delegation_metadata(parent_task_id=key, owner=current,
            task_labels=["Inspect"], resume_refs=["A"], resume_original_calls=[manifest], session_db=db)
        assert resumed["owner"] == original
        assert resumed["original_calls"] == metadata["original_calls"]
        admission = {**data, "attempt": 1, "resume_claim_id": "claim"}
        await cards.observe(source, "fixture", "tip", 2, "subagent.admitted", None, admission)
        assert "display_expires_at" not in card["original_calls"][manifest["id"]]
        assert card["attempt_history"]["A"]["0"]["terminal_at"] == 100
        assert card["owner"] == card["delegation_owner"] == original
        now[0] = 500
        await cards.observe(source, "fixture", "tip", 2, "subagent.tool", "read_file",
                            {**admission, "activity_sequence": 1})
        await cards.observe(source, "fixture", "tip", 2, "subagent.activity", None,
                            {**admission, "activity_sequence": 2, "activity_reason": "process"})
        row = card["rows"]["A"]
        assert row["last_tool"] == "read_file" and row["activity_reason"] == "process"
        before = deepcopy(row)
        # Stale attempts, out-of-order activity and noncanonical siblings cannot overwrite a wait.
        for actor, payload in [("tip", {**data, "activity_sequence": 99}),
                               ("tip", {**admission, "activity_sequence": 1}),
                               ("sibling", {**admission, "activity_sequence": 99})]:
            await cards.observe(source, "fixture", actor, 2, "subagent.activity", None,
                                {**payload, "activity_reason": "provider_capacity"})
        assert row == before
        await cards.observe(source, "fixture", "tip", 2, "subagent.complete", None,
                            {**admission, "activity_sequence": 3, "status": "completed"})
        await drain_cards(cards)
        assert "activity_reason" not in row and row["last_tool"] is None
        assert card["original_calls"][manifest["id"]]["display_expires_at"] == 800
        now[0] = 801
        await cards.reconcile()
        await drain_cards(cards)
        assert card["message_deleted"] and not card.get("handled") and not card.get("retired")
        assert card["original_calls"][manifest["id"]]["manifest"] == manifest
        retained = async_delegation.get_delegation_result(delegation_id, owner=current, session_db=db)
        assert retained is not None and retained["result"]["results"] == result["results"]
    finally:
        await cards.shutdown()
