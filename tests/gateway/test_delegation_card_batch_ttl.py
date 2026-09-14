"""Original-call display windows are independent of execution handling and anchors."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import delegation_card_presentation as presentation
from gateway.config import Platform
from gateway.delegation_cards import DelegationCards
from gateway.platforms.base import SendResult
from gateway.session import SessionSource


@pytest.fixture
def setup(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(presentation, "terminal_ttl_seconds", lambda manager, key: 30)
    adapter = SimpleNamespace(
        send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="msg")),
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    manager = DelegationCards(runner, home=tmp_path, interval=0, clock=lambda: now[0])
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    owner = dict(profile="default", session_id="s", session_key="route", chat_id="42", thread_id="8")
    return manager, source, owner, now, adapter


def event(owner, key, ref, call_id, refs, **kwargs):
    return dict(parent_task_id=key, thread_ref=ref, task_label=ref, owner=owner,
                original_call=dict(id=call_id, parent_task_id=key, member_refs=refs), **kwargs)


async def emit(manager, source, data, kind="start", **kwargs):
    await manager.observe(source, "route", "s", 1, "subagent." + kind, None, {**data, **kwargs})


async def drain(manager):
    for _ in range(30):
        tasks = list(manager.pending.values())
        if not tasks:
            return
        await asyncio.gather(*tasks)
    pytest.fail("presentation failed to settle")


def labels(manager, key):
    return {r["task_label"] for r in manager._display_projection(manager._anchor(key))["rows"].values()}


@pytest.mark.asyncio
async def test_complete_roster_blocks_early_expiry_and_handled_siblings_stay_visible(setup):
    manager, source, owner, now, adapter = setup
    key, call = "a" * 32, "b" * 32
    a = event(owner, key, "A", call, ["A", "B"])
    b = event(owner, key, "B", call, ["A", "B"])
    await emit(manager, source, a)
    await emit(manager, source, a, "complete", status="completed")
    await manager.handling(source, "route", "s", 1, actor_session_id="s", parent_task_id=key,
                           refs=["A"], reason="incorporated")
    await manager.delivered({key: manager._proof(manager.cards[key], ["A"])})
    await drain(manager)
    now[0] = 1000
    assert labels(manager, key) == {"A"}
    assert not manager._expiry_timers
    assert manager.cards[key]["handled"] == ["A"]
    await emit(manager, source, b)
    assert labels(manager, key) == {"A", "B"}
    now[0] = 1200
    await emit(manager, source, b, "complete", status="failed")
    assert labels(manager, key) == {"A", "B"}
    now[0] = 1229
    assert labels(manager, key) == {"A", "B"}
    now[0] = 1230
    assert labels(manager, key) == set()
    await manager.shutdown()
    await drain(manager)


@pytest.mark.asyncio
async def test_distinct_calls_sharing_parent_and_anchor_have_independent_deadlines(setup):
    manager, source, owner, now, _ = setup
    key = "c" * 32
    a = event(owner, key, "A", "d" * 32, ["A", "B"])
    b = event(owner, key, "B", "d" * 32, ["A", "B"])
    c = event(owner, key, "C", "e" * 32, ["C"])
    for data in (a, b, c):
        await emit(manager, source, data)
    await emit(manager, source, a, "complete", status="completed")
    await emit(manager, source, c, "complete", status="completed")
    now[0] = 131
    assert labels(manager, key) == {"A", "B"}
    now[0] = 500
    await emit(manager, source, b, "complete", status="completed")
    assert len(manager._expiry_timers) == 1
    now[0] = 530
    assert labels(manager, key) == set()
    await manager.shutdown()
    await drain(manager)


@pytest.mark.asyncio
async def test_restart_resume_reopens_original_batch_without_duplicate_or_config_reset(setup, tmp_path, monkeypatch):
    manager, source, owner, now, adapter = setup
    key, call = "f" * 32, "1" * 32
    a = event(owner, key, "A", call, ["A", "B"])
    b = event(owner, key, "B", call, ["A", "B"])
    for data in (a, b):
        await emit(manager, source, data)
    await emit(manager, source, a, "complete", status="completed")
    now[0] = 110
    await emit(manager, source, b, "complete", status="completed")
    await drain(manager)
    await manager.shutdown()
    monkeypatch.setattr(presentation, "terminal_ttl_seconds", lambda manager, key: 5)
    now[0] = 139
    restored = DelegationCards(manager.runner, home=tmp_path, interval=0, clock=lambda: now[0])
    await emit(restored, source, b, "complete", status="completed")
    assert labels(restored, key) == {"A", "B"}
    now[0] = 140
    assert labels(restored, key) == set()
    await emit(restored, source, a, "admitted", attempt=1, resume_claim_id="resume")
    assert labels(restored, key) == {"A", "B"}
    assert not restored._expiry_timers
    now[0] = 200
    await emit(restored, source, a, "complete", attempt=1, resume_claim_id="resume", status="completed")
    now[0] = 204
    assert labels(restored, key) == {"A", "B"}
    await emit(restored, source, a, "admitted", attempt=1, resume_claim_id="resume")
    now[0] = 205
    assert labels(restored, key) == set()
    await restored.shutdown()
    await drain(restored)


@pytest.mark.asyncio
async def test_hidden_root_member_and_nested_owner_keep_their_own_batch_open(setup):
    manager, source, owner, now, _ = setup
    key, call = "2" * 32, "3" * 32
    refs = list("ABCDEF")
    for ref in refs:
        await emit(manager, source, event(owner, key, ref, call, refs))
    # A falls outside the five-root window but is still part of the birth roster.
    manager.cards[key]["rows"]["A"]["state"] = "queued"
    for ref in refs[1:]:
        await emit(manager, source, event(owner, key, ref, call, refs), "complete", status="completed")
    nested_owner = {**owner, "session_id": "child-owner"}
    nested_key = "4" * 32
    nested = event(nested_owner, nested_key, "A", "5" * 32, ["A"],
                   card_owner=owner, card_parent_task_id=key, card_parent_thread_ref="F")
    nested["task_label"] = "Nested"
    await emit(manager, source, nested)
    now[0] = 1000
    assert labels(manager, key) == {*refs[1:], "Nested"}
    assert not manager._expiry_timers
    await emit(manager, source, event(owner, key, "A", call, refs), "complete", status="completed")
    now[0] = 1030
    assert labels(manager, key) == {"F", "Nested"}  # ancestor context only
    await emit(manager, source, nested, "complete", status="failed")
    now[0] = 1060
    assert labels(manager, key) == set()
    assert not manager.cards[key].get("handled")
    assert not manager.cards[nested_key].get("handled")
    await manager.shutdown()
    await drain(manager)


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [False, True])
async def test_batch_expiry_deletes_display_without_changing_retained_lifecycle(setup, acknowledged):
    import copy
    manager, source, owner, now, adapter = setup
    key, call = "6" * 32, "7" * 32
    data = event(owner, key, "A", call, ["A"])
    await emit(manager, source, data)
    await emit(manager, source, data, "complete", status="completed")
    if acknowledged:
        await manager.handling(source, "route", "s", 1, actor_session_id="s",
                               parent_task_id=key, refs=["A"], reason="incorporated")
        await manager.delivered({key: manager._proof(manager.cards[key], ["A"])})
    await drain(manager)
    adapter.delete_message.assert_not_awaited()
    before = copy.deepcopy({k: manager.cards[key].get(k) for k in ("rows", "handled", "retired", "handling")})
    scope = manager._scope(manager.cards[key])
    token = manager._expiry_tokens[scope]
    now[0] = 130
    manager._expiry_callback(scope, key, token)
    await drain(manager)
    adapter.delete_message.assert_awaited_once_with("42", "msg")
    assert {k: manager.cards[key].get(k) for k in before} == before
    assert labels(manager, key) == set()
    await manager.shutdown()


@pytest.mark.asyncio
async def test_acknowledged_batch_still_shares_anchor_with_later_unrelated_call(setup):
    manager, source, owner, now, adapter = setup
    key = "8" * 32
    first = event(owner, key, "A", "9" * 32, ["A"])
    await emit(manager, source, first)
    await emit(manager, source, first, "complete", status="completed")
    await manager.handling(source, "route", "s", 1, actor_session_id="s",
                           parent_task_id=key, refs=["A"], reason="incorporated")
    await manager.delivered({key: manager._proof(manager.cards[key], ["A"])})
    await drain(manager)
    other_key = "a" * 32
    second = event(owner, other_key, "B", "b" * 32, ["B"])
    await emit(manager, source, second)
    await drain(manager)
    assert manager._anchor(key) == manager._anchor(other_key)
    assert labels(manager, other_key) == {"A", "B"}
    assert adapter.send_delegation_card.await_count == 1
    assert manager.cards[key]["retired"]
    now[0] = 130
    assert labels(manager, other_key) == {"B"}
    await manager.shutdown()
    await drain(manager)
