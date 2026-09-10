"""Real manager, persistence, adapter and shared gate; only the Bot API is fake."""
import asyncio


async def handled_delivery(manager, key):
    card = manager.cards[key]
    await manager.handling(manager._source(card), "r", "s", 1, actor_session_id="s",
                           parent_task_id=key, refs=["A"], reason="incorporated")
    await manager.delivered({key: manager._proof(card, ["A"])})

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter, TimedOut

from gateway import delegation_card_anchor as anchor
from gateway.config import Platform, PlatformConfig
from gateway.delegation_cards import DelegationCards
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter


async def drain(manager):
    for _ in range(20):
        tasks = list(manager.pending.values())
        if not tasks:
            return
        await asyncio.gather(*tasks)
    raise AssertionError("unbounded scheduling")


async def fixture(tmp_path):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake", extra={"rich_messages": False}))
    adapter._send_cooldown_seconds = 0
    live = {}
    calls = []

    async def send(**kw):
        mid = str(len(calls) + 100)
        calls.append(("send", mid))
        live[mid] = kw
        return SimpleNamespace(message_id=int(mid))

    async def delete(**kw):
        mid = str(kw["message_id"])
        calls.append(("delete", mid))
        live.pop(mid, None)
        return True

    adapter._bot = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                                   edit_message_text=AsyncMock(return_value=True),
                                   delete_message=AsyncMock(side_effect=delete))
    manager = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    data = dict(parent_task_id="a" * 32, thread_ref="A", task_label="Check anchor", owner=dict(
        profile="default", session_id="s", session_key="r", chat_id="42", thread_id="8"))
    await manager.observe(source, "r", "s", 1, "subagent.start", None, data)
    await drain(manager)
    card = manager.cards[data["parent_task_id"]]
    return manager, adapter, source, data, card, live, calls


async def inbound(adapter, mid, topic="8"):
    await adapter._on_platform_update(SimpleNamespace(message=SimpleNamespace(
        chat_id=42, message_id=mid, message_thread_id=topic)), None)


@pytest.mark.asyncio
async def test_three_same_topic_messages_and_one_minute_fake_clock_trigger_one_move(tmp_path, monkeypatch):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    now = [1_000.0]
    monkeypatch.setattr(anchor.time, "time", lambda: now[0])
    card["anchored_at"] = 941.0
    manager.tracking_started = 900.0

    await inbound(adapter, 1, "other")
    await inbound(adapter, 2)
    await inbound(adapter, 2)  # Duplicate ingress never advances the ledger.
    await inbound(adapter, 3)
    await drain(manager)
    assert card["message_id"] in live
    assert len(manager.displacement[data["parent_task_id"]]) == 2

    # The third event is retained while the 60-second boundary is still closed.
    now[0] += 1
    await inbound(adapter, 4)
    await drain(manager)
    replacement = card["message_id"]
    assert replacement != "100"
    assert len([call for call in calls if call[0] == "send"]) == 2

    # Three new observations inside the following cooldown are not a timer or
    # a bypass: a later real event is required after the full provider-safe wait.
    for mid in (5, 6, 7):
        await inbound(adapter, mid)
    await drain(manager)
    assert card["message_id"] == replacement
    now[0] += anchor.COOLDOWN - 1
    await inbound(adapter, 8)
    await drain(manager)
    assert card["message_id"] == replacement
    now[0] += 1
    await inbound(adapter, 9)
    await drain(manager)
    assert card["message_id"] != replacement


@pytest.mark.asyncio
async def test_displacement_is_real_topic_activity_with_live_rows_and_original_identity(tmp_path):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    initial, started = card["message_id"], card["started_at"]
    card["anchored_at"] -= anchor.COOLDOWN
    manager.tracking_started -= anchor.COOLDOWN
    # Huge interleaved IDs in other topics, duplicate deliveries, status sends and
    # edited messages cannot constitute displacement in topic 8.
    for mid in range(900000, 900030):
        await inbound(adapter, mid, "other")
    for _ in range(20):
        await inbound(adapter, 700)
    await adapter._on_platform_update(SimpleNamespace(message=None, edited_message=object()), None)
    await drain(manager)
    assert card["message_id"] == initial
    assert len(manager.displacement[data["parent_task_id"]]) == 1
    # Interleave real outgoing conversation and ingress, without arithmetic on IDs.
    adapter._retrigger_typing = AsyncMock()
    await adapter.send("42", "Ordinary reply", metadata={"thread_id": "8"})
    for mid in range(701, 707):
        await inbound(adapter, mid)
    await drain(manager)
    replacement = card["message_id"]
    assert replacement != initial and initial not in live
    assert card["last_reanchor"]["new_message_id"] == replacement
    assert card["started_at"] == started and card["rows"]["A"]["display_ref"] == "A"
    assert not card.get("obsolete_message_id")
    assert json.loads(manager.path.read_text())[data["parent_task_id"]]["message_id"] == replacement
    # Cooldown is a predicate, not a timer that emits traffic on its own.
    for mid in range(800, 820):
        await inbound(adapter, mid)
    await drain(manager)
    assert card["message_id"] == replacement
    await manager.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
    card["anchored_at"] -= anchor.COOLDOWN
    manager.tracking_started -= anchor.COOLDOWN
    await inbound(adapter, 900)
    await drain(manager)
    assert card["message_id"] == replacement  # terminal-only never reanchors
    await handled_delivery(manager, data["parent_task_id"])
    await drain(manager)
    assert replacement not in live and card["retired"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["ambiguous", "cancel", "delete", "429", "receipt_restart"])
async def test_replace_failure_restart_and_cleanup_cannot_accumulate_anchors(tmp_path, failure):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    initial = card["message_id"]
    card["anchored_at"] -= anchor.COOLDOWN
    manager.tracking_started -= anchor.COOLDOWN
    original_send = adapter._bot.send_message.side_effect
    original_delete = adapter._bot.delete_message.side_effect
    attempts = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(**kw):
        nonlocal attempts
        attempts += 1
        if attempts == 1 and failure == "ambiguous":
            raise TimedOut("ambiguous")
        if attempts == 1 and failure == "429":
            raise RetryAfter(0.01)
        if attempts == 1 and failure == "cancel":
            entered.set()
            await release.wait()
        return await original_send(**kw)

    adapter._bot.send_message.side_effect = send
    if failure in {"delete", "receipt_restart"}:
        adapter._bot.delete_message.side_effect = RuntimeError("delete unavailable")
    for mid in range(anchor.DISPLACEMENT):
        await inbound(adapter, 1000 + mid)
    if failure == "cancel":
        await asyncio.wait_for(entered.wait(), 2)
        for task in list(manager.pending.values()):
            task.cancel()
        await asyncio.gather(*list(manager.pending.values()), return_exceptions=True)
    else:
        await drain(manager)
    if failure in {"ambiguous", "cancel"}:
        assert card["message_id"] == initial and initial in live
        assert card["reanchor"]["state"] == "attempting"
    elif failure in {"delete", "receipt_restart"}:
        assert card["message_id"] != initial and initial in live
        assert card["obsolete_message_id"] == initial
        if failure == "receipt_restart":
            # Simulate crash after durable new receipt, before active-ID switch.
            card["message_id"] = initial
            card.pop("obsolete_message_id")
            manager._save()
    else:
        assert attempts == 2 and card["message_id"] != initial and initial not in live
    sends = adapter._bot.send_message.await_count
    restored = DelegationCards(manager.runner, home=tmp_path, interval=0)
    await restored.reconcile()
    await drain(restored)
    recovered = restored.cards[data["parent_task_id"]]
    for mid in range(2000, 2020):
        await inbound(adapter, mid)
    await drain(restored)
    assert adapter._bot.send_message.await_count == sends
    assert recovered["rows"]["A"]["state"] == "unknown"
    if failure in {"delete", "receipt_restart"}:
        adapter._bot.delete_message.side_effect = original_delete
        await restored.reconcile()
        await drain(restored)
        assert initial not in live and not recovered.get("obsolete_message_id")
        assert not recovered.get("reanchor")
    # Late tool callbacks cannot revive a post-restart unknown execution.
    await restored.observe(source, "r", "s", 1, "subagent.tool", "terminal", data)
    assert recovered["rows"]["A"]["state"] == "unknown"
    await handled_delivery(restored, data["parent_task_id"])
    await drain(restored)
    assert not recovered.get("message_id")


@pytest.mark.asyncio
async def test_reanchor_coalesces_with_final_priority_and_terminal_callback(tmp_path):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    original = card["message_id"]
    card["anchored_at"] -= anchor.COOLDOWN
    manager.tracking_started -= anchor.COOLDOWN
    real_send = adapter._bot.send_message.side_effect
    final_entered, release = asyncio.Event(), asyncio.Event()

    async def send(**kw):
        if kw["text"] == "Final":
            final_entered.set()
            await release.wait()
        return await real_send(**kw)

    adapter._bot.send_message.side_effect = send
    adapter._retrigger_typing = AsyncMock()
    final = asyncio.create_task(adapter.send("42", "Final", metadata={"thread_id": "8"}))
    await final_entered.wait()
    for mid in range(1000, 1030):
        await inbound(adapter, mid)
    assert len(manager.pending) == 1
    # Final's actual API call owns the shared gate; no second API request races it.
    await asyncio.sleep(0)
    assert adapter._bot.send_message.await_count == 2  # initial card + blocked final
    release.set()
    await final
    await drain(manager)
    assert card["message_id"] != original and original not in live
    assert adapter._bot.send_message.await_count == 3
    # Late completion and final delivery operate on the logical task, not old ID.
    await manager.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
    await drain(manager)
    await handled_delivery(manager, data["parent_task_id"])
    await drain(manager)
    assert not card["message_id"]


def test_restart_does_not_resurrect_deleted_new_anchor(tmp_path):
    manager = DelegationCards(SimpleNamespace(), home=tmp_path)
    card = dict(owner={}, source={}, rows={}, started_at=1, message_id=None,
                message_deleted=True, obsolete_message_id="old", rendered="", retired=True,
                reanchor=dict(state="sent", old_message_id="old", new_message_id="new", rendered="", sent_at=2))
    manager.cards["a" * 32] = card
    manager._save()
    restored = DelegationCards(SimpleNamespace(), home=tmp_path)
    assert restored.cards["a" * 32]["message_id"] is None
    assert restored.cards["a" * 32]["obsolete_message_id"] == "old"


@pytest.mark.asyncio
@pytest.mark.parametrize("during", ["send", "delete"])
@pytest.mark.parametrize("final_delivery", [False, True])
async def test_event_during_replace_drains_without_another_external_event(tmp_path, during, final_delivery):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    initial = card["message_id"]
    card["anchored_at"] -= anchor.COOLDOWN
    manager.tracking_started -= anchor.COOLDOWN
    sent = adapter._bot.send_message.side_effect
    deleted = adapter._bot.delete_message.side_effect
    event_once = False
    final_tasks = []

    async def event():
        nonlocal event_once
        if event_once:
            return
        event_once = True
        # Exercise the accepted lifecycle mutation while a flush is pending,
        # as a reentrant transport callback. The public observe wrapper's lock
        # ordinarily serializes independent callbacks; this also verifies the
        # existing coalescing invariant itself, not only that lock's protection.
        await manager._observe(source, "r", "s", 1, "subagent.complete", None,
                               {**data, "status": "completed"})
        if final_delivery:
            final_tasks.append(asyncio.create_task(handled_delivery(manager, data["parent_task_id"])))

    async def send(**kw):
        if during == "send":
            await event()
        return await sent(**kw)

    async def delete(**kw):
        if during == "delete":
            await event()
        return await deleted(**kw)

    adapter._bot.send_message.side_effect = send
    adapter._bot.delete_message.side_effect = delete
    for mid in range(1000, 1010):
        await inbound(adapter, mid)
    await drain(manager)
    await asyncio.gather(*final_tasks)
    await drain(manager)
    assert initial not in live
    if final_delivery:
        assert not card["message_id"] and card["retired"]
    else:
        assert "Returned · awaiting parent" in card["rendered"]
        assert adapter._bot.edit_message_text.await_count >= 1
    assert not manager.pending
