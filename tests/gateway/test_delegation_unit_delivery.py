"""Independent unit arrival, coalescing, and explicit later owner-result retrieval."""
import asyncio
import json
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.delegation_disposition import begin_result_turn, observe_tool_results
from gateway.run_notifications import GatewayNotificationsMixin
from tests.gateway.test_delegation_handling import setup, drain
from tools import async_delegation as ad
from tools.delegate_tool import delegate_task
from tools.process_registry import process_registry


def metadata(data):
    return {"parent_task_id": data["parent_task_id"], "owner": {**data["owner"], "topic_id": ""},
            "thread_refs": ["A", "B"], "task_labels": ["First", "Second"], "attempts": {"A": 0, "B": 1},
            "threads": [{"thread_ref": "A", "task_index": 0, "task_label": "First"}, {"thread_ref": "B", "task_index": 1, "task_label": "Second"}]}


async def units(tmp_path, monkeypatch):
    cards, source, adapter, data, parent = await setup(tmp_path, monkeypatch)
    ad._reset_for_tests()
    meta = metadata(data)
    meta["owner_json"] = json.dumps(meta["owner"], sort_keys=True, separators=(",", ":"))
    releases, handles = [], []
    for i, ref in enumerate(("A", "B")):
        if i:
            await cards.observe(source, "r", "s", 1, "subagent.start", None, {**data, "thread_ref": ref, "attempt": i})
        await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "thread_ref": ref, "attempt": i, "status": "completed"})
        released = threading.Event()
        releases.append(released)
        def runner(index=i, gate=released):
            assert gate.wait(10)
            return {"results": [{"task_index": index, "status": "completed", "summary": f"Verified result {index}"}]}
        handle = ad.dispatch_async_delegation_batch(goals=["First", "Second"], context=None, toolsets=None, role="leaf", model="fixture", session_key="r", parent_session_id="s", runner=runner, task_indexes=[i], delegation_metadata=deepcopy(meta))
        assert handle["status"] == "dispatched"
        handles.append(handle)
    await drain(cards)
    return cards, source, data, parent, releases, handles


@pytest.mark.asyncio
async def test_independent_completions_gate_only_delivered_attempts_and_merge_exactly(tmp_path, monkeypatch):
    cards, source, data, parent, gates, handles = await units(tmp_path, monkeypatch)
    try:
        gates[0].set()
        evt_a = await asyncio.to_thread(process_registry.completion_queue.get, timeout=5)
        gates[1].set()
        evt_b = await asyncio.to_thread(process_registry.completion_queue.get, timeout=5)
        assert evt_a["thread_refs"] == ["A"] and evt_a["attempts"] == {"A": 0}
        assert evt_b["thread_refs"] == ["B"] and evt_b["attempts"] == {"B": 1}
        missing = await cards.result_turn(actor_session_id="s", turn_id="arrival-a", results=[evt_a])
        assert [r["thread_ref"] for r in missing["missing"]] == ["A"]
        captured = []
        runner = GatewayNotificationsMixin()
        runner._completion_identity_seen = lambda *a, **kw: False
        runner._completion_delivery_ready = AsyncMock(return_value=True)
        async def deliver(text, event, **kw):
            captured.append((text, event))
            return True
        runner._deliver_completion_notification = deliver
        assert await runner._deliver_async_delegation_group([evt_a, evt_b])
        text, combined = captured[0]
        assert "Verified result 0" in text and "Verified result 1" in text
        assert combined["thread_refs"] == ["A", "B"]
        assert combined["attempts"] == {"A": 0, "B": 1}
        missing = await cards.result_turn(actor_session_id="s", turn_id="combined", results=[combined])
        assert {r["thread_ref"] for r in missing["missing"]} == {"A", "B"}
        assert evt_a["attempts"] == {"A": 0}  # queue originals are immutable
    finally:
        for gate in gates:
            gate.set()
        await drain(cards)
        ad._reset_for_tests()


@pytest.mark.asyncio
async def test_early_failure_notice_has_only_failed_unit_identity(tmp_path, monkeypatch):
    cards, source, data, parent, gates, handles = await units(tmp_path, monkeypatch)
    try:
        ad.push_task_failure_notice(handles[1]["delegation_id"], {"task_index": 1, "status": "error", "error": "fixture failure"}, n_tasks=2)
        notice = await asyncio.to_thread(process_registry.completion_queue.get, timeout=5)
        assert notice["task_failure_notice"]
        assert notice["thread_refs"] == ["B"] and notice["attempts"] == {"B": 1}
        missing = await cards.result_turn(actor_session_id="s", turn_id="failure", results=[notice])
        assert [r["thread_ref"] for r in missing["missing"]] == ["B"]
    finally:
        for gate in gates:
            gate.set()
        for _ in gates:
            await asyncio.to_thread(process_registry.completion_queue.get, timeout=5)
        await drain(cards)
        ad._reset_for_tests()


@pytest.mark.asyncio
async def test_deferred_result_retrieved_by_exact_owner_in_later_ordinary_turn(tmp_path, monkeypatch):
    from gateway.session_context import set_session_vars, clear_session_vars
    cards, source, data, parent, gates, handles = await units(tmp_path, monkeypatch)
    tokens = set_session_vars(platform="telegram", profile="default", chat_id="42", session_key="r", session_id="s")
    try:
        for gate in gates:
            gate.set()
        events = [await asyncio.to_thread(process_registry.completion_queue.get, timeout=5) for _ in gates]
        evt_b = next(e for e in events if e["thread_refs"] == ["B"])
        await cards.result_turn(actor_session_id="s", turn_id="original", results=[evt_b])
        await cards.handling(source, "r", "s", 2, actor_session_id="s", parent_task_id=data["parent_task_id"], refs=["B"], reason="deferred", detail="Verify tomorrow", turn_id="original")
        parent._delegation_result_turn = "later-user-turn"
        assert not (await cards.result_turn(actor_session_id="s", turn_id="later-user-turn"))["missing"]
        refused = json.loads(await asyncio.to_thread(delegate_task, action="handle", parent_task_id=data["parent_task_id"], handled_refs=["B"], handling="incorporated", parent_agent=parent))
        assert "error" in refused  # no historical blanket gate/auto-presentation
        result = await asyncio.to_thread(delegate_task, action="result", delegation_id=evt_b["delegation_id"], parent_agent=parent)
        payload = json.loads(result)
        assert "error" not in payload, payload
        assert payload["thread_refs"] == ["B"] and payload["results"][0]["summary"] == "Verified result 1"
        assert cards.cards[data["parent_task_id"]]["handling"]["B"]["reason"] == "deferred"
        tool = SimpleNamespace(id="retrieve", function=SimpleNamespace(name="delegate_task"))
        await asyncio.to_thread(observe_tool_results, parent, SimpleNamespace(tool_calls=[tool]), [{"role": "tool", "tool_call_id": "retrieve", "content": result}])
        missing = await cards.result_turn(actor_session_id="s", turn_id="later-user-turn")
        assert [item["thread_ref"] for item in missing["missing"]] == ["B"]
        # Reading does not erase the old deferral, but a new explicit disposition is required.
        assert cards.cards[data["parent_task_id"]]["handling"]["B"]["reason"] == "deferred"
        assert cards.cards[data["parent_task_id"]]["result_turns"]["later-user-turn"] == {"B": 1}
        accepted = json.loads(await asyncio.to_thread(delegate_task, action="handle", parent_task_id=data["parent_task_id"], handled_refs=["B"], handling="incorporated", parent_agent=parent))
        assert accepted["recorded"] and accepted["awaiting_delivery"]
        parent.session_id = "foreign"
        foreign = json.loads(await asyncio.to_thread(delegate_task, action="result", delegation_id=evt_b["delegation_id"], parent_agent=parent))
        assert "error" in foreign and "Verified result" not in repr(foreign)
        assert cards._projection(data["parent_task_id"])["rows"]  # no delivery => no retirement
    finally:
        clear_session_vars(tokens)
        for gate in gates:
            gate.set()
        await drain(cards)
        ad._reset_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "no_async", "capacity", "mixed_inline"])
async def test_inline_terminal_results_remain_retrievable_after_deferral(tmp_path, monkeypatch, mode):
    import time
    from dataclasses import fields
    from gateway.session_context import set_session_vars, clear_session_vars
    from tools import delegate_tool_dispatch as dispatch
    from tools import delegate_tool as dt
    cards, source, adapter, data, parent = await setup(tmp_path, monkeypatch)
    ad._reset_for_tests()
    tokens = set_session_vars(platform="telegram", profile="default", chat_id="42", session_key="r", session_id="s")
    meta = metadata(data)
    meta["owner_json"] = json.dumps(meta["owner"], sort_keys=True, separators=(",", ":"))
    tasks = [{"goal": "First"}, {"goal": "Second"}]
    batch = dispatch._Batch(**{f.name: None for f in fields(dispatch._Batch)})
    batch.task_list, batch.parent_agent = tasks, parent
    batch.children = [(i, task, SimpleNamespace(session_id=f"child-{i}")) for i, task in enumerate(tasks)]
    batch.creds, batch.max_children, batch.top_role = {"model": "fixture"}, 2, "leaf"
    batch.live_paths, batch.live_writers = [], []
    batch.overall_start, batch.delegation_metadata = time.monotonic(), meta
    batch.origin_ui_session_id = "s"
    # Child model execution is the fixture boundary; aggregation, archive, owner
    # verification, presentation and disposition use real production code/SQLite.
    monkeypatch.setattr(dt, "_run_single_child", lambda task_index, **kw: {
        "task_index": task_index, "status": "completed", "summary": f"Inline result {task_index}"})
    monkeypatch.setattr(dispatch, "_resolve_async_wake_sid", lambda *a: None if mode == "no_async" else "")
    from tools import delegate_tool_config
    monkeypatch.setattr(delegate_tool_config, "_get_independent_completions", lambda: True)
    original_dispatch = dispatch._dispatch_unit
    def admit(unit, uid, slot, routing):
        if mode == "capacity" or (mode == "mixed_inline" and unit.children[0][0] == 1):
            return {"status": "rejected", "error": "fixture scheduler rejection"}
        return original_dispatch(unit, uid, slot, routing)
    monkeypatch.setattr(dispatch, "_dispatch_unit", admit)
    try:
        await cards.observe(source, "r", "s", 1, "subagent.start", None, {**data, "thread_ref": "B", "attempt": 1})
        await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "thread_ref": "B", "attempt": 1, "status": "completed"})
        await drain(cards)
        result = await asyncio.to_thread(dispatch._run_batch, batch, mode != "sync")
        payload = json.loads(result)
        entries = payload.get("inline_results", payload.get("results"))
        target = next(e for e in entries if e["task_index"] == 1)
        uid = target["result_delegation_id"]
        if mode == "mixed_inline":
            await asyncio.to_thread(process_registry.completion_queue.get, timeout=5)
        tool = SimpleNamespace(id="inline", function=SimpleNamespace(name="delegate_task"))
        parent._delegation_result_turn = "inline-turn"
        await asyncio.to_thread(observe_tool_results, parent, SimpleNamespace(tool_calls=[tool]),
                                [{"role": "tool", "tool_call_id": "inline", "content": result}])
        await cards.handling(source, "r", "s", 2, actor_session_id="s", parent_task_id=data["parent_task_id"],
                             refs=["B"], reason="deferred", detail="Check tomorrow", turn_id="inline-turn")
        parent._delegation_result_turn = "later-inline-turn"
        retrieved = await asyncio.to_thread(delegate_task, action="result", delegation_id=uid, parent_agent=parent)
        reread = json.loads(retrieved)
        assert "error" not in reread, reread
        assert next(e for e in reread["results"] if e["task_index"] == 1)["summary"] == "Inline result 1"
        assert reread["attempts"]["B"] == 1
        assert cards.cards[data["parent_task_id"]]["handling"]["B"]["reason"] == "deferred"
        await asyncio.to_thread(observe_tool_results, parent, SimpleNamespace(tool_calls=[tool]),
                                [{"role": "tool", "tool_call_id": "inline", "content": retrieved}])
        accepted = json.loads(await asyncio.to_thread(delegate_task, action="handle", parent_task_id=data["parent_task_id"],
            handled_refs=["B"], handling="incorporated", parent_agent=parent))
        assert accepted["recorded"] and accepted["awaiting_delivery"]
        assert cards._projection(data["parent_task_id"])["rows"]
        parent.session_id = "foreign"
        refused = json.loads(await asyncio.to_thread(delegate_task, action="result", delegation_id=uid, parent_agent=parent))
        assert "error" in refused and "Inline result" not in repr(refused)
        # Archives never manufacture a second async delivery event on restart.
        assert ad.get_durable_delegation(uid)["event"] is None
    finally:
        clear_session_vars(tokens)
        await drain(cards)
        ad._reset_for_tests()



@pytest.mark.asyncio
async def test_new_completion_reopens_owned_deferred_payload_without_acceptance(tmp_path, monkeypatch):
    from agent.delegation_disposition import begin_result_turn, _query
    from gateway.session_context import set_session_vars, clear_session_vars
    cards, source, data, parent, gates, handles = await units(tmp_path, monkeypatch)
    tokens = set_session_vars(platform="telegram", profile="default", chat_id="42", session_key="r", session_id="s")
    try:
        for gate in gates:
            gate.set()
        events = [await asyncio.to_thread(process_registry.completion_queue.get, timeout=5) for _ in gates]
        old = next(e for e in events if e["thread_refs"] == ["B"])
        new = next(e for e in events if e["thread_refs"] == ["A"])
        await cards.result_turn(actor_session_id="s", turn_id="old", results=[old])
        await cards.handling(source, "r", "s", 2, actor_session_id="s", parent_task_id=data["parent_task_id"], refs=["B"], reason="deferred", detail="Need approval", turn_id="old")
        content = await asyncio.to_thread(begin_result_turn, parent, {"delegation_results": [new]})
        assert "Verified result 1" in content
        assert old["delegation_id"] in content
        missing = (await asyncio.to_thread(_query, parent))["missing"]
        assert {item["thread_ref"] for item in missing} == {"A", "B"}
        assert cards.cards[data["parent_task_id"]]["handling"]["B"]["reason"] == "deferred"
        assert not cards.cards[data["parent_task_id"]].get("handled")
        await cards.handling(source, "r", "s", 3, actor_session_id="s", parent_task_id=data["parent_task_id"], refs=["B"], reason="deferred", detail="Need approval", turn_id=parent._delegation_result_turn)
        assert {item["thread_ref"] for item in (await asyncio.to_thread(_query, parent))["missing"]} == {"A"}
        assert not cards.cards[data["parent_task_id"]].get("handled")
    finally:
        clear_session_vars(tokens)
        for gate in gates:
            gate.set()
        await drain(cards)
        ad._reset_for_tests()
