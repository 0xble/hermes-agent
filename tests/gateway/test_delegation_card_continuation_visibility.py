"""Continuation admission owns rearming, never an ambiguous transport receipt."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.delegation_cards import DelegationCards
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from tests.gateway.test_delegation_cards import drain_cards, handling_receipt


async def completed_card(tmp_path, *, uncertain=False):
    adapter = SimpleNamespace(
        send_delegation_card=AsyncMock(return_value=SendResult(
            success=not uncertain, message_id=None if uncertain else "1")),
        edit_message=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        delete_message=AsyncMock(return_value=True))
    cards = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    data = dict(parent_task_id="a" * 32, thread_ref="A", task_label="Check route", child_session_id="child",
                owner=dict(profile="default", session_id="s", session_key="r", chat_id="42", thread_id=""))
    for event in ("subagent.start", "subagent.complete"):
        await cards.observe(source, "r", "s", 1, event, None, data)
        await drain_cards(cards)
    await cards.delivered(await handling_receipt(cards, data["parent_task_id"], ["A"], 1))
    await drain_cards(cards)
    return cards, adapter, source, data


@pytest.mark.asyncio
@pytest.mark.parametrize("live_sibling", [False, True])
async def test_confirmed_deleted_continuation_is_visible_once(tmp_path, live_sibling):
    cards, adapter, source, data = await completed_card(tmp_path)
    key = data["parent_task_id"]
    assert cards.cards[key]["message_deleted"]
    adapter.send_delegation_card.return_value = SendResult(success=True, message_id="2")
    if live_sibling:
        await cards.observe(source, "r", "s", 2, "subagent.start", None,
                            {**data, "parent_task_id": "b" * 32, "child_session_id": "sibling"})
        await drain_cards(cards)
    # Tombstone survives a process restart before the validated continuation.
    cards = DelegationCards(cards.runner, home=tmp_path, interval=0)
    admission = {**data, "attempt": 1, "resume_claim_id": "claim"}
    await cards.observe(source, "r", "s", 2, "subagent.admitted", None, admission)
    await drain_cards(cards)
    anchor = cards.cards[cards._anchor(key)]
    assert anchor["message_id"] == "2"
    assert "Check route" in anchor["rendered"]
    assert cards.cards[key]["attempt_history"]["A"]["0"]["state"] == "completed"
    await cards.observe(source, "r", "s", 2, "subagent.admitted", None, admission)
    await drain_cards(cards)
    assert adapter.send_delegation_card.await_count == 2
    # New presentation has a fresh deletion budget and receipt lifecycle.
    await cards.observe(source, "r", "s", 2, "subagent.complete", None, admission)
    await cards.delivered(await handling_receipt(cards, key, ["A"], 2))
    await drain_cards(cards)
    if not live_sibling:
        assert anchor["message_deleted"] and anchor["message_id"] is None
        assert adapter.delete_message.await_args.args[1] == "2"


@pytest.mark.asyncio
async def test_uncertain_send_continuation_restart_and_replay_never_duplicate(tmp_path):
    cards, adapter, source, data = await completed_card(tmp_path, uncertain=True)
    cards = DelegationCards(cards.runner, home=tmp_path, interval=0)
    admission = {**data, "attempt": 1, "resume_claim_id": "claim"}
    await cards.observe(source, "r", "s", 2, "subagent.admitted", None, admission)
    await drain_cards(cards)
    cards = DelegationCards(cards.runner, home=tmp_path, interval=0)
    await cards.reconcile()
    await cards.observe(source, "r", "s", 2, "subagent.admitted", None, admission)
    await drain_cards(cards)
    assert adapter.send_delegation_card.await_count == 1
    assert cards.cards[data["parent_task_id"]]["message_id"] is None
