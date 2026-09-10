"""Legacy persisted presentation recovery through real lifecycle/transport boundaries."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.delegation_cards import DelegationCards
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from gateway.config import Platform

E = "89e41c91b464443aa3aae62662617195"
CHILD = "1817e4e3876a4e69bbdd03c7275fec94"
G = "0e49c159235646cea8014e9d956ab3da"


def legacy():
    owner = dict(profile="default", session_id="20260907_132901_d15ec2ff",
                 session_key="agent:main:telegram:dm:2027045491:173511",
                 chat_id="2027045491", thread_id="173511")
    cards = {}
    for key, ref, display, profile, message, state in (
        (E, "E", "E", None, "85392", "interrupted"),
        (CHILD, "A", "E.1", None, None, "interrupted"),
        (G, "G", "G", "default", "85393", "failed"),
    ):
        row = dict(thread_ref=ref, display_ref=display, state=state,
                   task_label="Audit identity paths" if key == CHILD else "Run delegated task",
                   subagent_type="explorer" if key == CHILD else "lead", last_tool="terminal")
        if key == CHILD:
            row.update(card_parent_task_id=E, card_parent_thread_ref="E")
        cards[key] = dict(owner=copy.deepcopy(owner), delegation_owner=copy.deepcopy(owner),
                          source=dict(platform="telegram", chat_id=owner["chat_id"],
                                      thread_id=owner["thread_id"], profile=profile),
                          started_at=100 if key != G else 110, generation=5, receipt_epoch=4,
                          rows={ref: row}, retired=False, presentation_key=E if key == CHILD else key,
                          message_id=message, rendered="old", recoveries=0,
                          send_attempts=int(bool(message)))
    cards[CHILD]["delegation_owner"]["session_id"] = "20260909_213927_97d34f"
    return cards


def seed(home, data):
    path = home / "cache/delegation/cards.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


async def drain(manager):
    for _ in range(20):
        if not manager.pending:
            return
        await asyncio.gather(*list(manager.pending.values()))
    pytest.fail("presentation did not settle")


@pytest.mark.asyncio
async def test_exact_legacy_split_updates_survivor_before_delete_and_recovers(tmp_path):
    original = legacy()
    seed(tmp_path, original)
    messages = {"85392": "old E", "85393": "old G"}
    calls = []
    fail_edit, fail_delete = True, True

    async def edit(chat, message, text, **kwargs):
        calls.append(("edit", message))
        if fail_edit:
            return SendResult(success=False, error="transport unavailable")
        messages[message] = text
        return SendResult(success=True, message_id=message)

    async def delete(chat, message):
        calls.append(("delete", message))
        assert "E. Run delegated task" in messages["85392"]
        assert "\u00a0E.1. Audit identity paths" in messages["85392"]
        assert "G. Run delegated task" in messages["85392"]
        if fail_delete:
            return False
        messages.pop(message, None)
        return True

    adapter = SimpleNamespace(edit_message=edit, delete_message=delete,
                              send_delegation_card=AsyncMock())
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    manager = DelegationCards(runner, home=tmp_path, interval=0)
    await asyncio.gather(manager.reconcile(), manager.reconcile())
    await drain(manager)
    assert not any(c[0] == "delete" for c in calls)
    assert {c["presentation_key"] for c in manager.cards.values()} == {E}
    fail_edit = False
    await manager.reconcile()
    await drain(manager)
    assert "85393" in messages  # failed deletion remains retryable, never loses rows
    assert not adapter.send_delegation_card.called

    # Restart must recover the saved cleanup ledger, not create a replacement.
    fail_delete = False
    manager = DelegationCards(runner, home=tmp_path, interval=0)
    await manager.reconcile()
    await drain(manager)
    assert set(messages) == {"85392"}
    saved = json.loads(manager.path.read_text())
    for key in original:
        for field in ("owner", "delegation_owner", "rows", "retired"):
            assert saved[key][field] == original[key][field]
        assert not saved[key].get("handled")
    assert not adapter.send_delegation_card.called


@pytest.mark.asyncio
async def test_concurrent_dispatch_normalizes_only_same_profile_topic(tmp_path):
    adapter = SimpleNamespace(
        send_delegation_card=AsyncMock(side_effect=[SendResult(success=True, message_id=str(i)) for i in range(10)]),
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True))
    manager = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)

    async def start(key, profile, source_profile, topic):
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id=topic, profile=source_profile)
        owner = dict(profile=profile, session_id=key, session_key="route", chat_id="42", thread_id=topic)
        data = dict(parent_task_id=key * 32, thread_ref="A", task_label="Check", owner=owner)
        await manager.observe(source, "route", key, 1, "subagent.start", None, data)

    await asyncio.gather(start("a", "default", None, "topic"), start("b", "default", "default", "topic"),
                         start("c", "other", "other", "topic"), start("d", "default", None, "another"),
                         manager.reconcile())
    await drain(manager)
    assert adapter.send_delegation_card.call_count == 3
    assert manager.cards["a" * 32]["presentation_key"] == manager.cards["b" * 32]["presentation_key"]


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["sent", "attempting"])
async def test_consolidated_reanchor_receipts_survive_restart_without_replacement(tmp_path, receipt):
    from gateway import delegation_card_anchor as anchoring
    state = legacy()
    state[G]["reanchor"] = dict(state=receipt, old_message_id="85391",
                                new_message_id="85393", rendered="old", sent_at=1)
    if receipt == "sent":
        state[G]["obsolete_message_id"] = "85391"
    seed(tmp_path, state)
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(),
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=False))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    for _ in range(2):
        manager = DelegationCards(runner, home=tmp_path, interval=0)
        await manager.reconcile()
        await drain(manager)
        assert manager.cards[G]["message_id"] is None
        # Ordinary conversation displacement must not create another card while
        # cleanup is pending or a merged member has an ambiguous replacement.
        manager.cards[E]["rows"]["E"]["state"] = "running"
        manager.tracking_started = 0
        manager.displacement[E] = set(map(str, range(anchoring.DISPLACEMENT)))
        await asyncio.gather(manager.reconcile(), manager._flush(G))
        await drain(manager)
    assert not adapter.send_delegation_card.called
    entries = manager.cards[E]["presentation_cleanup"]
    assert {e["message_id"] for e in entries} == ({"85391", "85393"} if receipt == "sent" else {"85393"})
    assert all(e["attempts"] <= 3 for e in entries)


@pytest.mark.asyncio
async def test_ambiguous_send_blocks_concurrent_new_execution_and_restart(tmp_path):
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(side_effect=TimeoutError),
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    manager = DelegationCards(runner, home=tmp_path, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="173511")
    owner = dict(profile="default", session_id="s", session_key="r", chat_id="42", thread_id="173511")
    for key in ("a", "b"):
        await manager.observe(source, "r", "s", 1, "subagent.start", None,
                              dict(parent_task_id=key * 32, thread_ref="A", owner=owner))
        await drain(manager)
    restored = DelegationCards(runner, home=tmp_path, interval=0)
    await restored.reconcile()
    await drain(restored)
    assert adapter.send_delegation_card.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["scoped", "invalid", "boolean", "float", "dangling"])
async def test_exact_cleanup_policy_preserves_unapproved_messages(tmp_path, policy):
    state = legacy()
    for key, card in list(state.items()):
        other = copy.deepcopy(card)
        other["source"]["thread_id"] = "other"
        other["presentation_key"] += "-other"
        if other["message_id"]:
            other["message_id"] += "-other"
        state[key + "-other"] = other
    seed(tmp_path, state)
    path = tmp_path / "cache/delegation/presentation-cleanup-policy.json"
    path.write_text(json.dumps({"version": 1, "allow": [dict(profile="default", platform="telegram",
        chat_id=state[E]["source"]["chat_id"], thread_id="173511", message_id="85393")]}))
    if policy in ("boolean", "float", "invalid"):
        malformed = json.loads(path.read_text())
        malformed["version"] = {"boolean": True, "float": 1.0, "invalid": "*"}[policy]
        path.write_text(json.dumps(malformed))
    elif policy == "dangling":
        path.unlink()
        path.symlink_to(tmp_path / "missing-policy")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(),
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    for _ in range(2):
        manager = DelegationCards(runner, home=tmp_path, interval=0)
        await manager.reconcile()
        await drain(manager)
    if policy == "scoped":
        adapter.delete_message.assert_awaited_once_with(state[E]["source"]["chat_id"], "85393")
    else:
        adapter.delete_message.assert_not_awaited()
    assert not adapter.send_delegation_card.called

