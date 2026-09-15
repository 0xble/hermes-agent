"""Read-only reproduction: durable terminal result outruns its card observation."""
import asyncio
import copy
import json
import queue
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.delegation_cards import DelegationCards
from gateway.delegation_delivery_receipt import delivery_metadata_for_event
from gateway.platforms.base import SendResult
from gateway.run_notifications import GatewayNotificationsMixin
from gateway.session import SessionSource, SessionStore
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import async_delegation as ad


async def inject_notification(tmp_path, source, adapter, event):
    captured = []

    async def accept(message):
        message._gateway_accepted = True
        captured.append(message)

    adapter.handle_message = accept
    runner = GatewayNotificationsMixin()
    runner.session_store = SessionStore(tmp_path / "sessions", GatewayConfig())
    entry = runner.session_store.get_or_create_session(source)
    runner.session_store._entries[event["session_key"]] = entry
    runner._adapter_for_source = lambda _: adapter
    assert await runner._inject_watch_notification("Recorded result", event) is True
    assert len(captured) == 1
    return delivery_metadata_for_event(captured[0], "input-owner")


@pytest.mark.asyncio
@pytest.mark.parametrize("card_completion_committed", [False, True])
async def test_recovered_terminal_result_resolves_original_call_card(tmp_path, monkeypatch, card_completion_committed):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = [100.0]
    adapter = SimpleNamespace(
        send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="card")),
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    manager = DelegationCards(runner, home=tmp_path, interval=0, clock=lambda: now[0])
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    owner = dict(profile="default", session_id="parent", session_key="route", chat_id="42", thread_id="8")
    key, call = "a" * 32, "b" * 32
    data = dict(parent_task_id=key, thread_ref="A", task_label="Work", owner=owner,
                attempt=0, child_session_id="child",
                original_call=dict(id=call, parent_task_id=key, member_refs=["A"]))
    await manager.observe(source, "route", "parent", 1, "subagent.start", None, data)
    for task in list(manager.pending.values()):
        await task
    complete = manager.observe(source, "route", "parent", 1, "subagent.complete", None,
                               {**data, "status": "completed"})
    lock = manager.locks[manager._scope(manager.cards[key])]
    if card_completion_committed:
        await complete
        pending = None
    else:
        # Production schedules completion without waiting. A busy scope lock is
        # sufficient to let the worker persist its result before card mutation.
        await lock.acquire()
        pending = asyncio.create_task(complete)
        await asyncio.sleep(0)
        assert not pending.done()
    metadata = dict(parent_task_id=key, owner=owner,
                    owner_json=json.dumps(owner, sort_keys=True, separators=(",", ":")),
                    threads=[dict(thread_ref="A", task_index=0, original_call_id=call)], attempts={"A": 0})
    record = dict(delegation_id="deleg_recovered_card", session_key="route", parent_session_id="parent",
                  dispatched_at=time.time(), completed_at=time.time(), goal="Work", goals=["Work"],
                  is_batch=True, delegation_metadata=metadata)
    ad._persist_dispatch(record)
    result = {"results": [{"task_index": 0, "status": "completed", "child_session_id": "child",
                            "summary": "Finished", "exit_reason": "completed"}]}
    ad._push_completion_event(record, result, "completed")
    durable = ad.get_delegation_result(record["delegation_id"], owner=owner)
    assert durable["state"] == "completed"
    assert durable["result"] == result
    if pending is not None:
        # Process death loses the queued observation, not the committed ledger.
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        lock.release()
    await manager.shutdown()
    manager = DelegationCards(runner, home=tmp_path, interval=0, clock=lambda: now[0])
    try:
        restored = queue.Queue()
        assert ad.restore_undelivered_completions(restored) == 1
        event = restored.get_nowait()
        assert event["restored"] and event["results"][0]["child_session_id"] == "child"
        presented = await inject_notification(tmp_path, source, adapter, event)
        await manager.result_turn(actor_session_id="parent", actor_owner=owner, turn_id="result-turn",
                                  results=presented["delegation_results"])
        await manager.handling(source, "route", "parent", 2, actor_session_id="parent",
                               parent_task_id=key, refs=["A"], reason="incorporated", turn_id="result-turn")
        await manager.delivered({key: manager._proof(manager.cards[key], ["A"])})
        assert manager.cards[key]["handled"] == ["A"]
        now[0] = 10**10
        card = manager.cards[key]
        projection = manager._display_projection(manager._anchor(key))
        print(json.dumps({"ledger_state": durable["state"], "card_state": card["rows"]["A"]["state"],
                          "handled": card["handled"], "retired": card["retired"],
                          "call_expiry": card["original_calls"][call].get("display_expires_at"),
                          "visible_rows": len(projection["rows"])}))
        assert card["rows"]["A"]["state"] == "completed"
        assert not projection["rows"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "mixed", "resumed", "budget_exhausted", "foreign_owner", "foreign_profile",
    "foreign_parent", "foreign_ref", "stale_attempt", "superseded", "wrong_child",
    "missing_child", "unknown_status", "missing_entry", "duplicate_entry",
    "duplicate_thread", "duplicate_units", "wrong_call", "missing_attempt",
    "missing_delivery_id", "missing_ledger", "live_ledger", "malformed_status",
])
async def test_terminal_reconciliation_requires_exact_execution_evidence(tmp_path, monkeypatch, case):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = SimpleNamespace(
        send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="card")),
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    manager = DelegationCards(runner, home=tmp_path, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    owner = dict(profile="default", session_id="parent", session_key="route", chat_id="42", thread_id="8")
    key, call = "a" * 32, "b" * 32
    for ref in ("A", "B"):
        data = dict(parent_task_id=key, thread_ref=ref, task_label=ref, owner=owner,
                    attempt=0, child_session_id="child-" + ref,
                    original_call=dict(id=call, parent_task_id=key, member_refs=["A", "B"]))
        await manager.observe(source, "route", "parent", 1, "subagent.start", None, data)
    if case in {"resumed", "superseded"}:
        data = {**data, "thread_ref": "A", "child_session_id": "child-A"}
        await manager.observe(source, "route", "parent", 1, "subagent.complete", None,
                              {**data, "status": "budget_exhausted", "result_turn_id": "prior-child-turn"})
        await manager.observe(source, "route", "parent", 1, "subagent.admitted", None,
                              {**data, "attempt": 1, "resume_claim_id": "resume-claim"})
    await manager.shutdown()
    manager = DelegationCards(runner, home=tmp_path, interval=0)
    try:
        card = manager.cards[key]
        attempt = card["rows"]["A"]["attempt"]
        locators = dict(parent_task_id=key, thread_refs=["A", "B"], attempts={"A": attempt, "B": 0})
        await manager.result_turn(actor_session_id="parent", actor_owner=owner, turn_id="prior",
                                  results=[locators])
        await manager.handling(source, "route", "parent", 2, actor_session_id="parent",
                               parent_task_id=key, refs=["A"], reason="deferred", detail="approval", turn_id="prior")
        preserved = copy.deepcopy({field: card.get(field) for field in
                                   ("handling", "handled", "attempt_history", "receipt_epoch")})
        prior_presentations = copy.deepcopy(card["result_turns"]["prior"])
        metadata = dict(parent_task_id=key, owner=owner,
                        threads=[dict(thread_ref=ref, task_index=i, original_call_id=call)
                                 for i, ref in enumerate(("A", "B"))], attempts={"A": attempt, "B": 0})
        children = [dict(task_index=0, status="completed", child_session_id="child-A", summary="Done"),
                    dict(task_index=1, status="unknown", child_session_id="child-B", error="Owner exited")]
        if case == "foreign_owner":
            metadata["owner"] = {**owner, "session_id": "foreign"}
        elif case == "foreign_parent":
            metadata["parent_task_id"] = "c" * 32
        elif case == "foreign_ref":
            metadata["threads"][0]["thread_ref"] = "Z"
        elif case == "stale_attempt":
            metadata["attempts"]["A"] = attempt + 1
        elif case == "superseded":
            metadata["attempts"]["A"] = 0
        elif case == "wrong_child":
            children[0]["child_session_id"] = "other-child"
        elif case == "missing_child":
            children[0].pop("child_session_id")
        elif case in {"unknown_status", "budget_exhausted", "malformed_status"}:
            children[0]["status"] = {"unknown_status": "unknown", "budget_exhausted": "budget_exhausted",
                                      "malformed_status": ["completed"]}[case]
        elif case == "missing_entry":
            children.pop(0)
        elif case == "duplicate_entry":
            children.append({**children[0], "status": "failed"})
        elif case == "duplicate_thread":
            metadata["threads"].append(dict(metadata["threads"][0]))
        elif case == "wrong_call":
            metadata["threads"][0]["original_call_id"] = "d" * 32
        elif case == "missing_attempt":
            metadata["attempts"].pop("A")
        metadata["owner_json"] = json.dumps(metadata["owner"], sort_keys=True, separators=(",", ":"))
        record = dict(delegation_id="deleg_evidence", session_key="route", parent_session_id="parent",
                      dispatched_at=time.time(), completed_at=time.time(), goal="Work", goals=["A", "B"],
                      is_batch=True, delegation_metadata=metadata)
        token = set_hermes_home_override(tmp_path / "foreign-profile" if case == "foreign_profile" else tmp_path)
        try:
            if case != "missing_ledger":
                ad._persist_dispatch(record)
                if case != "live_ledger":
                    ad._push_completion_event(record, {"results": children}, "unknown")
            if case == "duplicate_units":
                ad._persist_dispatch({**record, "delegation_id": "deleg_duplicate"})
                ad._push_completion_event({**record, "delegation_id": "deleg_duplicate"}, {"results": children}, "unknown")
            stored = ad.get_durable_delegation(record["delegation_id"])
            event = dict((stored or {}).get("event") or {
                "type": "async_delegation", "delegation_id": record["delegation_id"], "session_key": "route",
            })
        finally:
            reset_hermes_home_override(token)
        # Even a notification claiming valid presentation locators cannot make
        # conflicting, foreign or missing ledger evidence authoritative.
        event.update(parent_task_id=key, owner=owner, thread_refs=["A", "B"], attempts=locators["attempts"])
        if case == "missing_delivery_id":
            event.pop("delegation_id")
        if case == "duplicate_units":
            event["delegation_deliveries"] = [{"delegation_id": did, "owner": owner}
                                             for did in ("deleg_evidence", "deleg_duplicate")]
        presented = await inject_notification(tmp_path, source, adapter, event)
        await manager.result_turn(actor_session_id="parent", actor_owner=owner, turn_id="recovered",
                                  results=presented["delegation_results"])
        expected = "completed" if case in {"mixed", "resumed"} else "budget_exhausted" if case == "budget_exhausted" else "unknown"
        assert card["rows"]["A"]["state"] == expected
        assert card["rows"]["B"]["state"] == "unknown"
        assert "display_expires_at" not in card["original_calls"][call]
        assert {field: card.get(field) for field in preserved} == preserved
        assert card["result_turns"]["prior"] == prior_presentations
        if expected != "unknown":
            assert "result_turn_id" not in card["rows"]["A"]
            terminal_at = card["rows"]["A"]["terminal_at"]
            await manager.result_turn(actor_session_id="parent", actor_owner=owner, turn_id="recovered",
                                      results=presented["delegation_results"])
            assert card["rows"]["A"]["terminal_at"] == terminal_at
        else:
            await manager.observe(source, "route", "parent", 1, "subagent.complete", None,
                                  dict(parent_task_id=key, thread_ref="A", owner=owner, attempt=attempt,
                                       child_session_id="child-A", status="completed"))
            assert card["rows"]["A"]["state"] == "unknown"
    finally:
        await manager.shutdown()
