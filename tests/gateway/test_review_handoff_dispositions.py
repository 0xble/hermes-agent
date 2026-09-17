"""Review phase boundaries retain exact result obligations, not handling authority."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from agent.delegation_disposition import begin_result_turn, finish_result_turn, _query
from gateway import delivery_ledger
from gateway.delegation_cards import DelegationCards
from gateway.run import _normalize_empty_agent_response
from gateway.session_context import set_session_vars, clear_session_vars
from tests.gateway.test_delegation_handling import drain
from tests.gateway.test_delegation_unit_delivery import units
from tests.gateway.test_review_handoff_response import _agent_result, _gateway_result
from tools import async_delegation as ad
from tools.delegate_tool import delegate_task
from tools.process_registry import process_registry


@pytest.mark.asyncio
@pytest.mark.parametrize("restart, handoffs, previously_deferred", [
    (False, 1, False), (True, 1, False), (True, 2, False), (True, 2, True),
])
async def test_handoff_result_represented_before_next_answer(tmp_path, monkeypatch, restart, handoffs, previously_deferred):
    cards, source, data, parent, gates, _ = await units(tmp_path, monkeypatch)
    tokens = set_session_vars(platform="telegram", profile="default", chat_id="42", session_key="r", session_id="s")
    correction = Mock(side_effect=AssertionError("No correction at a review handoff"))
    try:
        for gate in gates:
            gate.set()
        events = [await asyncio.to_thread(process_registry.completion_queue.get, timeout=5) for _ in gates]
        old = next(e for e in events if e["thread_refs"] == ["B"])
        key = data["parent_task_id"]
        assert isinstance(key, str)
        if previously_deferred:
            await cards.result_turn(actor_session_id="s", turn_id="earlier", results=[old])
            await cards.handling(source, "r", "s", 2, actor_session_id="s", parent_task_id=key,
                                 refs=["B"], reason="deferred", detail="Under review", turn_id="earlier")
        prior_handling = dict(cards.cards[key].get("handling", {}))
        agent = MagicMock(session_id="s", tool_progress_callback=parent.tool_progress_callback, _session_db=None)
        await asyncio.to_thread(begin_result_turn, agent, {"delegation_results": [old]})
        for _ in range(handoffs):
            previous_turn = agent._delegation_result_turn
            # Real tool-round exit -> finalizer -> disposition boundary -> gateway normalization.
            result = await asyncio.to_thread(_agent_result, agent=agent)
            result = await asyncio.to_thread(finish_result_turn, agent, result, correction, None, "parent")
            assert result["turn_exit_reason"] == "review_dispatched"
            assert result["final_response"] == "" and not result.get("delegation_disposition_unresolved")
            normalized = _gateway_result(result)
            assert _normalize_empty_agent_response(normalized, normalized["final_response"]) == ""
            correction.assert_not_called()
            assert cards.cards[key].get("handling", {}) == prior_handling
            assert not cards.cards[key].get("handled") and not cards.cards[key].get("retired")
            assert {r["thread_ref"] for r in cards._projection(key)["rows"].values()} == {"A", "B"}
            if restart:
                await drain(cards)
                cards = cards.runner._delegation_cards = DelegationCards(cards.runner, home=tmp_path, interval=0)
                # No in-memory agent presentation queue survives a restart.
                agent = MagicMock(session_id="s", tool_progress_callback=parent.tool_progress_callback, _session_db=None)
            # No child arrival is necessary: review completion or an ordinary turn can resume processing.
            content = await asyncio.to_thread(begin_result_turn, agent, None)
            assert "Verified result 1" in content
            assert old["delegation_id"] in content and "Verified result 0" not in content
            assert agent._delegation_result_turn != previous_turn
            assert cards.cards[key]["result_turns"][agent._delegation_result_turn] == {"B": 1}
            assert (await asyncio.to_thread(_query, agent))["missing"] == [
                {"parent_task_id": key, "thread_ref": "B", "attempt": 1, "task_label": data["task_label"]}]
            assert cards.cards[key].get("handling", {}) == prior_handling
            assert not cards.cards[key].get("handled") and not cards.cards[key].get("retired")
        accepted = json.loads(await asyncio.to_thread(delegate_task, action="handle", parent_agent=agent,
            parent_task_id=key, handled_refs=["B"], handling="incorporated"))
        assert accepted["recorded"] and accepted["awaiting_delivery"]
        assert "B" not in cards.cards[key].get("review_handoffs", {})
        result = await asyncio.to_thread(finish_result_turn, agent,
            {"final_response": "Verified result incorporated", "messages": [], "api_calls": 1}, correction, None, "parent")
        assert result["final_response"] == "Verified result incorporated"
        correction.assert_not_called()
        proof = cards.receipt(SimpleNamespace(source=source), "r", 2)
        assert proof and not cards.cards[key].get("handled")
        delivery_ledger.record_obligation(obligation_id="answer", session_key="r", platform="telegram", chat_id="42",
            thread_id=None, content=result["final_response"], delegation_receipt=proof)
        delivery_ledger.mark_failed("answer", "transport unavailable")
        await cards.reconcile()
        assert not cards.cards[key].get("handled")
        delivery_ledger.mark_delivered("answer")
        await cards.reconcile()
        assert cards.cards[key]["handled"] == ["B"]
        assert {r["thread_ref"] for r in cards._projection(key)["rows"].values()} == {"A"}
        assert await asyncio.to_thread(begin_result_turn, agent, None) == ""
    finally:
        clear_session_vars(tokens)
        for gate in gates:
            gate.set()
        await drain(cards)
        ad._reset_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["session_id", "chat_id", "attempt", "unpresented"])
async def test_handoff_does_not_reopen_unrelated_owner_or_attempt(tmp_path, monkeypatch, mismatch):
    cards, source, data, parent, gates, _ = await units(tmp_path, monkeypatch)
    tokens = set_session_vars(platform="telegram", profile="default", chat_id="42", session_key="r", session_id="s")
    try:
        for gate in gates:
            gate.set()
        events = [await asyncio.to_thread(process_registry.completion_queue.get, timeout=5) for _ in gates]
        old = next(e for e in events if e["thread_refs"] == ["B"])
        await asyncio.to_thread(begin_result_turn, parent, {"delegation_results": [old]})
        await asyncio.to_thread(finish_result_turn, parent,
            {"final_response": "", "turn_exit_reason": "review_dispatched"}, Mock(), None, "parent")
        key = data["parent_task_id"]
        assert isinstance(key, str)
        # Offering a persisted locator never grants this turn handling authority.
        offered = await cards.result_turn(actor_session_id="s", turn_id="locator-only", include_handoffs=True)
        assert [x["thread_ref"] for x in offered["handoffs"]] == ["B"]
        with pytest.raises(ValueError, match="absent or superseded"):
            await cards.handling(source, "r", "s", 2, actor_session_id="s", parent_task_id=key,
                                 refs=["B"], reason="incorporated", turn_id="locator-only")
        if mismatch == "session_id":
            parent.session_id = "foreign"
        elif mismatch == "chat_id":
            clear_session_vars(tokens)
            tokens = set_session_vars(platform="telegram", profile="default", chat_id="foreign", session_key="r", session_id="s")
        elif mismatch == "attempt":
            # A real admitted continuation supersedes only the old attempt.
            resumed = {**data, "thread_ref": "B", "attempt": 2, "resume_claim_id": "new-claim"}
            await cards.observe(source, "r", "s", 2, "subagent.admitted", None, resumed)
            await cards.observe(source, "r", "s", 2, "subagent.complete", None, {**resumed, "status": "completed"})
            assert cards.cards[key]["rows"]["B"]["attempt"] == 2
        content = await asyncio.to_thread(begin_result_turn, parent, None)
        if mismatch == "unpresented":
            assert "Verified result 1" in content and "Verified result 0" not in content
            ref = "A"
        else:
            assert content == ""
            ref = "B"
        rejected = json.loads(await asyncio.to_thread(delegate_task, action="handle", parent_agent=parent,
            parent_task_id=key, handled_refs=[ref], handling="incorporated"))
        assert "error" in rejected
        assert not cards.cards[key].get("handled")
    finally:
        clear_session_vars(tokens)
        for gate in gates:
            gate.set()
        await drain(cards)
        ad._reset_for_tests()
