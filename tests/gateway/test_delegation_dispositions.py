"""Real card/delivery ledgers; only platform I/O and model responses are fixtures."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.delegation_disposition import begin_result_turn, finish_result_turn, observe_tool_results
from gateway.delegation_cards import DelegationCards, render_card
from tests.gateway.test_delegation_handling import setup, drain


async def terminal(cards, source, data, ref="A", state="completed", **extra):
    data = {**data, "thread_ref": ref, **extra}
    await cards.observe(source, "r", "s", 1, "subagent.start", None, data)
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": state})
    await drain(cards)
    return data


@pytest.mark.asyncio
async def test_omitted_partial_batch_and_unrelated_turn(tmp_path, monkeypatch):
    cards, source, _, data, parent = await setup(tmp_path, monkeypatch)
    await terminal(cards, source, data)
    await terminal(cards, source, data, ref="B")
    await terminal(cards, source, data, ref="C")
    presented = [{"parent_task_id": data["parent_task_id"], "thread_refs": ["A", "B"]}]
    missing = await cards.result_turn(actor_session_id="s", turn_id="returned", results=presented)
    assert [m["thread_ref"] for m in missing["missing"]] == ["A", "B"]
    await cards.handling(source, "r", "s", 2, actor_session_id="s", parent_task_id=data["parent_task_id"],
                         refs=["A"], reason="incorporated", turn_id="returned")
    assert [m["thread_ref"] for m in (await cards.result_turn(actor_session_id="s", turn_id="returned"))["missing"]] == ["B"]
    assert await cards.result_turn(actor_session_id="s", turn_id="unrelated-user-question") == {"missing": []}
    assert await cards.result_turn(actor_session_id="other", turn_id="returned", results=presented) == {"missing": []}
    restored = DelegationCards(cards.runner, home=tmp_path, interval=0)
    assert [m["thread_ref"] for m in (await restored.result_turn(actor_session_id="s", turn_id="returned"))["missing"]] == ["B"]


@pytest.mark.asyncio
async def test_deferred_reason_visible_persisted_not_delivery_receipt(tmp_path, monkeypatch):
    cards, source, _, data, _ = await setup(tmp_path, monkeypatch)
    await terminal(cards, source, data, state="interrupted")
    for reason in (None, "", " ", "x" * 161):
        with pytest.raises(ValueError, match="short nonempty"):
            await cards.handling(source, "r", "s", 2, actor_session_id="s", parent_task_id=data["parent_task_id"],
                                 refs=["A"], reason="deferred", detail=reason)
    await cards.result_turn(actor_session_id="s", turn_id="t", results=[{"parent_task_id": data["parent_task_id"], "thread_refs": ["A"]}])
    await cards.handling(source, "r", "s", 2, actor_session_id="s", parent_task_id=data["parent_task_id"],
                         refs=["A"], reason="deferred", detail="Await explicit authorization", turn_id="t")
    await drain(cards)
    assert await cards.result_turn(actor_session_id="s", turn_id="t") == {"missing": []}
    assert cards.receipt(None, "r", 2) == {}
    restored = DelegationCards(cards.runner, home=tmp_path, interval=0)
    text = render_card(restored.cards[data["parent_task_id"]])
    assert "Ⅱ" in text and "Deferred · Await explicit authorization" in text
    assert data["parent_task_id"] not in text and not restored.cards[data["parent_task_id"]].get("handled")


@pytest.mark.asyncio
async def test_continuation_same_row_exact_attempt_idempotent_and_stale_completion(tmp_path, monkeypatch):
    cards, source, _, data, _ = await setup(tmp_path, monkeypatch)
    # Legacy fixture row lacks child ID; production rows bind it at initial start.
    row = cards.cards[data["parent_task_id"]]["rows"]["A"]
    row["child_session_id"] = "child"
    await terminal(cards, source, data, state="interrupted")
    await cards.result_turn(actor_session_id="s", turn_id="old-turn", results=[{"parent_task_id": data["parent_task_id"], "thread_refs": ["A"]}])
    before = dict(row)
    # Construction/start is not admission and may not erase the prior result.
    resumed = {**data, "attempt": 1, "resume_claim_id": "claim", "child_session_id": "child", "task_label": "Recover renamed"}
    await cards.observe(source, "r", "s", 2, "subagent.start", None, resumed)
    assert row == before
    await cards.observe(source, "r", "s", 2, "subagent.admitted", None, resumed)
    await drain(cards)
    assert row["state"] == "running" and row["task_label"] == before["task_label"]
    assert row["child_session_id"] == "child" and row["attempt"] == 1
    assert await cards.result_turn(actor_session_id="s", turn_id="old-turn") == {"missing": []}
    restored = DelegationCards(cards.runner, home=tmp_path, interval=0)
    await restored.observe(source, "r", "s", 2, "subagent.admitted", None, resumed)
    await restored.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
    assert restored.cards[data["parent_task_id"]]["rows"]["A"]["state"] == "unknown"
    await restored.observe(source, "r", "s", 2, "subagent.complete", None, {**resumed, "status": "completed"})
    await drain(restored)
    # New terminal attempt cannot inherit the old revision disposition.
    result = await restored.result_turn(actor_session_id="s", turn_id="new-turn", results=[{
        "parent_task_id": data["parent_task_id"], "thread_refs": ["A"], "attempts": {"A": 1}}])
    assert result["missing"][0]["attempt"] == 1
    history = restored.cards[data["parent_task_id"]]["attempt_history"]["A"]["0"]
    assert history["disposition"]["reason"] == "revision_requested"
    assert len(restored.cards[data["parent_task_id"]]["rows"]) == 1
    with pytest.raises(ValueError, match="absent or superseded"):
        await restored.handling(source, "r", "s", 2, actor_session_id="s",
                                parent_task_id=data["parent_task_id"], refs=["A"],
                                reason="incorporated", turn_id="old-turn")
    with pytest.raises(ValueError, match="handling reason"):
        await restored.handling(source, "r", "s", 2, actor_session_id="s",
                                parent_task_id=data["parent_task_id"], refs=["A"], reason="revision_requested")


@pytest.mark.asyncio
async def test_replacement_claim_blocks_duplicate_and_uncertain_launch(tmp_path, monkeypatch):
    cards, source, _, data, _ = await setup(tmp_path, monkeypatch)
    await terminal(cards, source, data, state="failed")
    kwargs = dict(actor_session_id="s", parent_task_id=data["parent_task_id"], refs=["A"], reason="validate_replacement")
    claim = await cards.handling(source, "r", "s", 2, **kwargs)
    restored = DelegationCards(cards.runner, home=tmp_path, interval=0)
    with pytest.raises(ValueError, match="already reserved"):
        await restored.handling(source, "r", "s", 2, **kwargs)
    await restored.handling(source, "r", "s", 2, **{**kwargs, "reason": "release_replacement"}, detail="wrong")
    with pytest.raises(ValueError, match="already reserved"):
        await restored.handling(source, "r", "s", 2, **kwargs)
    await restored.handling(source, "r", "s", 2, **{**kwargs, "reason": "release_replacement"}, detail=claim["claim_id"])
    retry = await restored.handling(source, "r", "s", 2, **kwargs)
    assert retry["claim_id"] != claim["claim_id"]
    assert not restored.cards[data["parent_task_id"]].get("handled")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["handle", "omit", "exception"])
async def test_bounded_boundary_correction_preserves_original_answer(tmp_path, monkeypatch, mode):
    cards, source, _, data, _ = await setup(tmp_path, monkeypatch)
    await terminal(cards, source, data)
    loop = asyncio.get_running_loop()
    def callback(event, **kw):
        assert event == "subagent.result_turn"
        return asyncio.run_coroutine_threadsafe(cards.result_turn(**kw), loop).result(timeout=5)
    agent = SimpleNamespace(session_id="s", tool_progress_callback=callback, max_iterations=12,
                            _emit_warning=Mock(side_effect=RuntimeError("warning failed") if mode == "exception" else None))
    metadata = {"delegation_results": [{"parent_task_id": data["parent_task_id"], "thread_refs": ["A"]}]}
    await asyncio.to_thread(begin_result_turn, agent, metadata)
    original_messages = [{"role": "user", "content": "result"}, {"role": "assistant", "content": "Original answer"}]
    calls = []
    def run_turn(a, prompt, system, history, task, **kw):
        calls.append(prompt)
        assert history is original_messages and a.max_iterations == 2
        assert history[-1]["role"] == "assistant" and kw["persist_user_display_kind"] == "hidden"
        assert a._delegation_disposition_correction == {data["parent_task_id"]: ["A"]}
        if mode == "exception":
            raise TimeoutError("fixture provider unavailable")
        if mode == "handle":
            asyncio.run_coroutine_threadsafe(cards.handling(source, "r", "s", 2, actor_session_id="s",
                parent_task_id=data["parent_task_id"], refs=["A"], reason="incorporated", turn_id=a._delegation_result_turn), loop).result(timeout=5)
        return {"final_response": "Discard this repair prose", "messages": history, "api_calls": 1}
    result = await asyncio.to_thread(finish_result_turn, agent,
        {"final_response": "Original answer", "messages": original_messages, "api_calls": 1}, run_turn, "Stable system", "task")
    assert len(calls) == 1 and result["final_response"].startswith("Original answer")
    assert "Discard" not in result["final_response"] and agent.max_iterations == 12
    assert agent._delegation_disposition_correction is None
    assert bool(result.get("delegation_disposition_unresolved")) == (mode != "handle")
    assert bool(cards.receipt(SimpleNamespace(source=source), "r", 2)) == (mode == "handle")
    assert not cards.cards[data["parent_task_id"]].get("handled")


def test_correction_tool_guard_and_terminal_tool_result_provenance():
    from agent.tool_executor import _internal_turn_effect_block
    recorded = []
    agent = SimpleNamespace(session_id="s", _delegation_result_turn="t", _delegation_result_tracking_error=False,
                            tool_progress_callback=lambda event, **kw: recorded.append(kw) or {"missing": []},
                            _delegation_disposition_correction={"key": ["A"]})
    args = dict(action="handle", parent_task_id="key", handled_refs=["A"], handling="deferred", defer_reason="Wait")
    assert _internal_turn_effect_block(agent, "delegate_task", args) is None
    for name, payload in [("terminal", {}), ("delegate_task", {**args, "action": "spawn"}),
                          ("delegate_task", {**args, "handled_refs": ["B"]})]:
        assert _internal_turn_effect_block(agent, name, payload)
    call = SimpleNamespace(tool_calls=[SimpleNamespace(id="call", function=SimpleNamespace(name="delegate_task"))])
    for payload in [{"status": "dispatched", "delegation_metadata": {"parent_task_id": "key", "thread_refs": ["A"]}},
                    {"results": [{"task_index": 1}], "delegation_metadata": {"parent_task_id": "key", "thread_refs": ["A", "B"]}}]:
        observe_tool_results(agent, call, [{"role": "tool", "tool_call_id": "call", "content": json.dumps(payload)}])
    assert len(recorded) == 1 and recorded[0]["results"][0]["thread_refs"] == ["B"]
