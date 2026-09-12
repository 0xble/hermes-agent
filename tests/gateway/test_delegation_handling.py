"""Exact parent handling, real ledger dependency and transport lifecycle."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.delegation_cards import DelegationCards
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from tools.delegate_tool import delegate_task


async def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    cards = runner._delegation_cards = DelegationCards(runner, home=tmp_path, interval=0)
    owner = dict(profile="default", session_id="s", session_key="r", chat_id="42", thread_id="")
    data = dict(parent_task_id="a" * 32, thread_ref="A", task_label="Check handling", owner=owner)
    ctx = TurnContext(source=source, session_id="s", session_key="r", run_generation=2,
                      _loop_for_step=asyncio.get_running_loop())
    relay = TurnRunner(runner, ctx)
    parent = SimpleNamespace(session_id="s", tool_progress_callback=relay.progress_callback)
    await cards.observe(source, "r", "s", 1, "subagent.start", None, data)
    await drain(cards)
    return cards, source, adapter, data, parent


async def drain(cards):
    while cards.pending:
        await asyncio.gather(*list(cards.pending.values()))


async def attest(parent, key, refs=("A",), reason="incorporated"):
    result = json.loads(await asyncio.to_thread(delegate_task, action="handle", parent_task_id=key,
                        handled_refs=list(refs), handling=reason, parent_agent=parent))
    assert result.get("recorded"), result
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["completed", "failed", "interrupted"])
async def test_handled_terminal_waits_for_success_and_survives_restart(tmp_path, monkeypatch, state):
    from gateway import delivery_ledger as ledger
    cards, source, adapter, data, parent = await setup(tmp_path, monkeypatch)
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": state})
    await drain(cards)
    event = MessageEvent(source=source, text="result", internal=True,
                         metadata={"delegation_parent_task_id": data["parent_task_id"], "delegation_thread_refs": ["A"]})
    assert cards.receipt(event, "r", 2) == {}  # arrival is not incorporation
    assert cards.cards[data["parent_task_id"]]["rows"]["A"]["state"] == state
    await attest(parent, data["parent_task_id"], reason="blocker_report" if state != "completed" else "incorporated")
    proof = cards.receipt(event, "r", 2)
    assert proof and not cards.cards[data["parent_task_id"]].get("handled")
    ledger.record_obligation(obligation_id="outbound", session_key="r", platform="telegram", chat_id="42",
                             thread_id=None, content="Handled result", delegation_receipt=proof)
    ledger.mark_failed("outbound", "send_path_degraded")
    restored = DelegationCards(cards.runner, home=tmp_path, interval=0)
    await restored.reconcile()
    await drain(restored)
    assert restored._projection(data["parent_task_id"])["rows"]
    adapter.delete_message.assert_not_awaited()
    # A retrying parent can attest again before its new final is recorded. That
    # must not invalidate the original, still-pending delivery obligation.
    await restored.handling(source, "r", "s", 99, actor_session_id="s",
                            parent_task_id=data["parent_task_id"], refs=["A"], reason="incorporated")
    assert restored._proof(restored.cards[data["parent_task_id"]], ["A"])["handling_ids"] == proof[data["parent_task_id"]]["handling_ids"]
    restored = DelegationCards(cards.runner, home=tmp_path, interval=0)
    ledger.mark_delivered("outbound")
    await restored.reconcile()
    await drain(restored)
    assert not restored._projection(data["parent_task_id"])["rows"]
    adapter.delete_message.assert_awaited_once_with("42", "1")
    assert restored.cards[data["parent_task_id"]]["rows"]["A"]["state"] == state
    again = DelegationCards(cards.runner, home=tmp_path, interval=0)
    await again.reconcile()
    await again.observe(source, "r", "s", 3, "subagent.complete", None, data)
    assert not again._projection(data["parent_task_id"])["rows"]


@pytest.mark.asyncio
async def test_confirmed_replacement_only_and_same_anchor(tmp_path, monkeypatch):
    cards, source, adapter, data, parent = await setup(tmp_path, monkeypatch)
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "failed"})
    claim = await cards.handling(source, "r", "s", 2, actor_session_id="s", parent_task_id=data["parent_task_id"],
                         refs=["A"], reason="validate_replacement")
    assert not cards.cards[data["parent_task_id"]].get("handled")
    # Validation / a failed spawn cannot create a handling proof.
    new = {**data, "parent_task_id": "b" * 32, "task_label": "Retry operation",
           "replaces": {"parent_task_id": data["parent_task_id"], "thread_ref": "A",
                        "claim_id": claim["claim_id"], "attempt": claim["attempt"]}}
    await cards.observe(source, "r", "s", 2, "subagent.start", None, new)
    await drain(cards)
    assert not cards.cards[data["parent_task_id"]].get("handled")
    await cards.observe(source, "r", "s", 2, "subagent.admitted", None, new)
    await drain(cards)
    assert cards.cards[data["parent_task_id"]]["handled"] == ["A"]
    assert cards.cards[data["parent_task_id"]]["rows"]["A"]["state"] == "failed"
    adapter.send_delegation_card.assert_awaited_once()
    assert cards.cards["b" * 32]["rows"]["A"]["replaces"] == new["replaces"]
    assert "Retry operation" in adapter.edit_message.call_args.args[2]


@pytest.mark.asyncio
async def test_owner_isolation_and_grouping_ancestor(tmp_path, monkeypatch):
    cards, source, adapter, data, parent = await setup(tmp_path, monkeypatch)
    child = {**data, "parent_task_id": "b" * 32, "owner": {**data["owner"], "session_id": "child"},
             "card_owner": data["owner"], "card_parent_task_id": data["parent_task_id"], "card_parent_thread_ref": "A"}
    await cards.observe(source, "r", "s", 1, "subagent.start", None, child)
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
    await attest(parent, data["parent_task_id"])
    event = MessageEvent(source=source, text="done")
    await cards.delivered(cards.receipt(event, "r", 2))
    projection = cards._projection(data["parent_task_id"])
    assert len(projection["rows"]) == 2  # handled grouping ancestor remains
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**child, "status": "failed"})
    with pytest.raises(ValueError, match="exact parent"):
        await cards.handling(source, "r", "s", 2, actor_session_id="s", parent_task_id="b" * 32,
                             refs=["A"], reason="incorporated")
    assert cards.receipt(event, "r", 2) == {}  # root handling never cascades
    await cards.result_turn(actor_session_id="child", turn_id="nested-turn",
                            results=[{"parent_task_id": child["parent_task_id"], "thread_refs": ["A"]}])
    await cards.handling(source, "r", "s", 2, actor_session_id="child", parent_task_id="b" * 32,
                         refs=["A"], reason="incorporated", turn_id="nested-turn")
    await drain(cards)
    assert cards._projection(data["parent_task_id"])["rows"]  # no early nested acceptance
    root_row = cards.cards[data["parent_task_id"]]["rows"]["A"]
    root_row.update(child_session_id="child", result_turn_id="nested-turn")
    await cards.result_turn(actor_session_id="s", turn_id="root-result", results=[{
        "parent_task_id": data["parent_task_id"], "thread_refs": ["A"]}])
    await drain(cards)
    assert not cards._projection(data["parent_task_id"])["rows"]


@pytest.mark.asyncio
async def test_registry_action_cannot_handle_running_or_other_parent(tmp_path, monkeypatch):
    cards, source, adapter, data, parent = await setup(tmp_path, monkeypatch)
    from tools.registry import registry
    payload = dict(action="handle", parent_task_id=data["parent_task_id"], handled_refs=["A"], handling="incorporated")
    result = json.loads(await asyncio.to_thread(registry.dispatch, "delegate_task", payload, parent_agent=parent))
    assert "error" in result
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, data)
    parent.session_id = "other"
    result = json.loads(await asyncio.to_thread(registry.dispatch, "delegate_task", payload, parent_agent=parent))
    assert "error" in result
    assert not cards.cards[data["parent_task_id"]].get("handling")
    await drain(cards)


@pytest.mark.asyncio
async def test_spawn_failure_after_replacement_validation_retains_old(tmp_path, monkeypatch):
    from tests.tools.test_delegate_required_labels import _valid_runtime
    from tools import delegate_tool
    cards, source, adapter, data, parent = await setup(tmp_path, monkeypatch)
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "failed"})
    _valid_runtime(monkeypatch)
    monkeypatch.setattr(delegate_tool, "_build_children", lambda *_a, **_k: (None, "fixture constructor failure"))
    parent._delegate_depth = 0
    raw = await asyncio.to_thread(delegate_tool.delegate_task, parent_agent=parent, tasks=[{
        "goal": "Replacement fixture", "task_label": "Check fixture", "replaces": {"parent_task_id": data["parent_task_id"], "thread_ref": "A"}}])
    assert json.loads(raw)["error"] == "fixture constructor failure"
    await drain(cards)
    assert not cards.cards[data["parent_task_id"]].get("handled")
    assert not cards.cards[data["parent_task_id"]].get("handling")
    adapter.delete_message.assert_not_awaited()

@pytest.mark.asyncio
async def test_silent_blocker_is_not_a_delivered_report(tmp_path, monkeypatch):
    from gateway.run_turn import GatewayTurnMixin
    cards, source, adapter, data, parent = await setup(tmp_path, monkeypatch)
    cards.runner._should_send_voice_reply = lambda *_a, **_kw: False
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "failed"})
    await attest(parent, data["parent_task_id"], reason="blocker_report")
    await GatewayTurnMixin._hmwa_deliver_turn_response(cards.runner, MessageEvent(source=source, text=""), source,
        SimpleNamespace(session_id="s"), "r", 2, {}, [], "", None, True)
    await drain(cards)
    assert not cards.cards[data["parent_task_id"]].get("handled")
    adapter.delete_message.assert_not_awaited()

