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
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

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


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", ["42", "-42"])
async def test_anchor_cleanup_reserves_shared_budget_and_preserves_server_cooldown(tmp_path, chat):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    calls.clear()
    adapter._send_cooldown_seconds = 2.0
    adapter._send_cooldown_group_seconds = 6.0
    live["100"] = {}
    before = anchor.time.monotonic()
    assert await adapter.delete_message(chat, "100")
    assert adapter._send_cooldown_until[chat] >= before + adapter._chat_send_gap(chat)
    adapter._send_cooldown_until[chat] = anchor.time.monotonic() + 3600
    live["101"] = {}
    assert await adapter._delete_status_message(chat, "101") is None
    assert "101" in live
    assert calls == [("delete", "100")]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["reanchor", "current", "obsolete"])
@pytest.mark.parametrize("policy", ["exclude", "malformed", "symlink"])
async def test_cleanup_policy_guards_every_physical_boundary_and_survives_reload(tmp_path, boundary, policy):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    key = data["parent_task_id"]
    original = card["message_id"]
    path = manager.path.with_name("presentation-cleanup-policy.json")
    path.write_text('{"version": 1, "allow": []}' if policy == "exclude" else "{")
    if policy == "symlink":
        path.unlink()
        path.symlink_to(tmp_path / "absent")
    if boundary == "reanchor":
        for mid in range(1, 7):
            await inbound(adapter, mid)
        await drain(manager)
    elif boundary == "current":
        card["rows"]["A"]["state"] = "completed"
        await handled_delivery(manager, key)
    else:
        card["obsolete_message_id"] = "999"
        live["999"] = {}
        manager._save()
        await manager._delete_obsolete(key)
    await drain(manager)
    assert not [call for call in calls if call[0] == "delete"]
    assert len([call for call in calls if call[0] == "send"]) == 1
    assert card.get("delete_attempts", 0) == 0
    assert card.get("obsolete_delete_attempts", 0) == 0
    assert (card.get("reanchor") or {}).get("delete_attempts", 0) == 0

    restored = DelegationCards(manager.runner, home=tmp_path, interval=0)
    await restored.reconcile()
    await drain(restored)
    assert not [call for call in calls if call[0] == "delete"]
    if path.is_symlink():
        path.unlink()
    approved = "999" if boundary == "obsolete" else original
    path.write_text(json.dumps({"version": 1, "allow": [dict(profile="default", platform="telegram",
        chat_id="42", thread_id="8", message_id=approved)]}))
    if boundary == "reanchor":
        await anchor.replace(restored, key)
    elif boundary == "obsolete":
        await restored._delete_obsolete(key)
    else:
        await restored.reconcile()
    await drain(restored)
    assert [call for call in calls if call[0] == "delete"] == [("delete", approved)]


async def inbound(adapter, mid, topic="8"):
    await adapter._on_platform_update(SimpleNamespace(message=SimpleNamespace(
        chat_id=42, message_id=mid, message_thread_id=topic)), None)


@pytest.mark.asyncio
async def test_six_same_topic_messages_count_both_directions_without_time_gate(tmp_path, monkeypatch):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    now = 1_000.0
    monkeypatch.setattr(anchor.time, "time", lambda: now)
    card["anchored_at"] = manager.tracking_started = now
    initial = card["message_id"]
    adapter._retrigger_typing = AsyncMock()
    await inbound(adapter, 1, "other")
    await inbound(adapter, 2)
    await inbound(adapter, 2)  # Duplicate ingress never advances the ledger.
    await adapter._on_platform_update(SimpleNamespace(message=None, edited_message=object()), None)
    for mid in (3, 4, 5):
        await inbound(adapter, mid)
    await adapter.send("42", "Ordinary reply", metadata={"thread_id": "8"})
    await drain(manager)
    assert len(manager.displacement[data["parent_task_id"]]) == 5
    assert card["message_id"] == initial
    await inbound(adapter, 6)
    await drain(manager)
    replacement = card["message_id"]
    assert replacement != initial and initial not in live
    assert data["parent_task_id"] not in manager.displacement  # Card self-send excluded.
    # A second six-message window can move immediately; no clock advancement.
    for mid in range(10, 15):
        await inbound(adapter, mid)
    await drain(manager)
    assert card["message_id"] == replacement
    await inbound(adapter, 15)
    await drain(manager)
    assert card["message_id"] != replacement and replacement not in live


@pytest.mark.asyncio
async def test_displacement_is_real_topic_activity_with_live_rows_and_original_identity(tmp_path):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    initial, started = card["message_id"], card["started_at"]
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
    assert json.loads(manager.path.read_text(encoding="utf-8"))[data["parent_task_id"]]["message_id"] == replacement
    await manager.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
    for mid in range(800, 820):
        await inbound(adapter, mid)
    await drain(manager)
    assert card["message_id"] == replacement
    await inbound(adapter, 900)
    await drain(manager)
    assert card["message_id"] == replacement  # terminal-only never reanchors
    await handled_delivery(manager, data["parent_task_id"])
    await drain(manager)
    assert replacement not in live and card["retired"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["normal", "ambiguous", "cancel", "delete", "delete_cancel",
                                     "429", "delete_429", "not_found", "reject", "repeated_429"])
async def test_replace_failure_restart_and_cleanup_cannot_accumulate_anchors(tmp_path, failure):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    initial = card["message_id"]
    original_send = adapter._bot.send_message.side_effect
    original_delete = adapter._bot.delete_message.side_effect
    attempts = deletes = 0
    entered = asyncio.Event()

    def persisted():
        return json.loads(manager.path.read_text(encoding="utf-8"))[data["parent_task_id"]]

    async def send(**kw):
        nonlocal attempts
        attempts += 1
        assert initial not in live
        assert persisted()["reanchor"]["state"] == "sending"
        if failure == "ambiguous":
            await original_send(**kw)  # accepted remotely, receipt lost
            raise TimedOut("ambiguous")
        if failure == "repeated_429" or (attempts == 1 and failure == "429"):
            raise RetryAfter(0.01)
        if attempts == 1 and failure == "reject":
            raise Forbidden("definitely rejected")
        if failure == "cancel":
            await original_send(**kw)  # cancellation after acceptance also fences
            entered.set()
            await asyncio.Event().wait()
        result = await original_send(**kw)
        assert len(live) <= 1
        return result

    async def delete(**kw):
        nonlocal deletes
        deletes += 1
        assert persisted()["reanchor"]["state"] == "deleting"
        if failure == "delete":
            raise TimedOut("deletion unknown")
        if failure == "delete_cancel":
            await original_delete(**kw)
            entered.set()
            await asyncio.Event().wait()
        if deletes == 1 and failure == "delete_429":
            raise RetryAfter(0.01)
        if failure == "not_found":
            live.pop(initial, None)
            raise BadRequest("Message to delete not found")
        result = await original_delete(**kw)
        assert len(live) <= 1
        return result

    adapter._bot.send_message.side_effect = send
    adapter._bot.delete_message.side_effect = delete
    for mid in range(anchor.DISPLACEMENT):
        await inbound(adapter, 1000 + mid)
    if failure in {"cancel", "delete_cancel"}:
        await asyncio.wait_for(entered.wait(), 2)
        tasks = list(manager.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    else:
        await drain(manager)
    assert len(live) <= 1
    if failure in {"ambiguous", "cancel"}:
        assert card["message_id"] is None and initial not in live
        assert card["reanchor"]["state"] == "sending"
    elif failure in {"delete", "delete_cancel"}:
        assert attempts == 0
        assert card["reanchor"]["state"] == "deleting"
    elif failure == "repeated_429":
        assert attempts == 3 and not live
    else:
        assert card["message_id"] != initial and initial not in live
        assert calls[1][0] == "delete" or failure == "not_found"
    sends = adapter._bot.send_message.await_count
    adapter._bot.delete_message.side_effect = original_delete
    restored = DelegationCards(manager.runner, home=tmp_path, interval=0)
    await restored.reconcile()
    await drain(restored)
    recovered = restored.cards[data["parent_task_id"]]
    for mid in range(2000, 2020):
        await inbound(adapter, mid)
    await drain(restored)
    assert adapter._bot.send_message.await_count == sends
    assert recovered["rows"]["A"]["state"] == "unknown"
    assert len(live) <= 1
    await restored.observe(source, "r", "s", 1, "subagent.tool", "terminal", data)
    assert recovered["rows"]["A"]["state"] == "unknown"
    await handled_delivery(restored, data["parent_task_id"])
    await drain(restored)
    assert not recovered.get("message_id")


@pytest.mark.asyncio
async def test_reanchor_coalesces_with_final_priority_and_terminal_callback(tmp_path):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    original = card["message_id"]
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
    sent = adapter._bot.send_message.side_effect
    deleted = adapter._bot.delete_message.side_effect
    event_once = False
    final_tasks = []

    async def event():
        nonlocal event_once
        if event_once:
            return
        event_once = True
        # Real lifecycle calls must not block behind the transport gap.
        await manager.observe(source, "r", "s", 1, "subagent.complete", None,
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
    elif during == "send":
        assert "Awaiting parent" in card["rendered"]
        assert adapter._bot.edit_message_text.await_count >= 1
    else:
        assert card["message_id"] is None
        assert adapter._bot.send_message.await_count == 1
    assert not manager.pending



@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["tool", "terminal", "handled"])
async def test_replacement_renders_after_final_priority_wait(tmp_path, event):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    original_send = adapter._bot.send_message.side_effect
    original_delete = adapter._bot.delete_message.side_effect
    final_entered, release = asyncio.Event(), asyncio.Event()
    finals = []
    adapter._retrigger_typing = AsyncMock()

    async def send(**kw):
        if kw["text"] == "Final":
            final_entered.set()
            await release.wait()
        return await original_send(**kw)

    async def delete(**kw):
        result = await original_delete(**kw)
        finals.append(asyncio.create_task(adapter.send("42", "Final", metadata={"thread_id": "8"})))
        await asyncio.sleep(0)  # register final waiter while deletion owns the gate
        return result

    adapter._bot.send_message.side_effect = send
    adapter._bot.delete_message.side_effect = delete
    for mid in range(1000, 1006):
        await inbound(adapter, mid)
    await asyncio.wait_for(final_entered.wait(), 2)
    assert not live  # old card gone; final and replacement not accepted yet
    if event == "tool":
        await manager.observe(source, "r", "s", 1, "subagent.tool", "read_file", data)
    else:
        await manager.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
        if event == "handled":
            await handled_delivery(manager, data["parent_task_id"])
    release.set()
    await asyncio.gather(*finals)
    await drain(manager)
    cards = [kw for kw in live.values() if kw["text"] != "Final"]
    assert len(cards) <= 1
    if event == "tool":
        assert "read_file" in card["rendered"]
        assert len(cards) == 1
    else:
        assert not cards and card["message_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["delete_pending", "deleting", "deleted", "sending", "sent", "legacy_attempting", "legacy_sent"])
async def test_restart_phase_receipts_preserve_transport_and_later_topic_owner(tmp_path, phase):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    old = card["message_id"]
    if phase.startswith("legacy"):
        receipt = dict(state=phase.removeprefix("legacy_"), old_message_id=old)
    else:
        receipt = dict(order="delete_first", state=phase, old_message_id=old,
                       delete_attempts=1, send_attempts=1)
    if phase in {"deleted", "sending", "sent"}:
        live.pop(old)
        card.update(message_id=None, message_deleted=True, send_attempts=1, rendered="")
    if phase in {"sent", "legacy_sent"}:
        live["999"] = {"text": "receipt"}
        receipt.update(new_message_id="999", rendered="receipt", sent_at=1)
        card.pop("message_deleted", None)
    if phase == "sending":
        live["998"] = {"text": "unacknowledged remote acceptance"}
    card["reanchor"] = receipt
    manager._save()
    restored = DelegationCards(manager.runner, home=tmp_path, interval=0)
    await restored.reconcile()
    await drain(restored)
    assert adapter._bot.send_message.await_count == 1
    recovered = restored.cards[data["parent_task_id"]]
    await handled_delivery(restored, data["parent_task_id"])
    await drain(restored)
    # A distinct live task cannot sidestep an uncertain old send, even after
    # final delivery retires that task's row. Definite deletion may resume.
    await restored.observe(source, "r", "s", 1, "subagent.start", None,
                           {**data, "parent_task_id": "b" * 32, "task_label": "Next task"})
    await drain(restored)
    if phase in {"sending", "legacy_attempting"}:
        assert adapter._bot.send_message.await_count == 1
        assert recovered["reanchor"]["state"] in {"sending", "attempting"}
    else:
        assert adapter._bot.send_message.await_count == 2
    assert len(live) <= 1



@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("failure", ["reject", "429", "ambiguous"])
@pytest.mark.parametrize("new_work", ["start", "same_row"])
async def test_exhausted_replacement_new_work_recovers_without_bypassing_uncertainty(tmp_path, restart, failure, new_work):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    original_send = adapter._bot.send_message.side_effect
    attempts = 0

    async def reject(**kw):
        nonlocal attempts
        deadline = json.loads(manager.path.read_text())[data["parent_task_id"]].get("reanchor", {}).get("retry_not_before", 0)
        assert anchor.time.time() >= deadline
        attempts += 1
        if failure == "ambiguous":
            await original_send(**kw)
            raise TimedOut("accepted but receipt lost")
        if failure == "429":
            raise RetryAfter(0.01)
        raise Forbidden("definite rejection")

    adapter._bot.send_message.side_effect = reject
    for mid in range(anchor.DISPLACEMENT):
        await inbound(adapter, 1000 + mid)
    await drain(manager)
    assert attempts == (1 if failure == "ambiguous" else 3)
    if restart:
        manager = DelegationCards(manager.runner, home=tmp_path, interval=0)
        await manager.reconcile()
        await drain(manager)
    card = manager.cards[data["parent_task_id"]]
    # Replayed starts and ordinary tool progress cannot replenish the budget.
    await manager.observe(source, "r", "s", 1, "subagent.start", None, data)
    await manager.observe(source, "r", "s", 1, "subagent.tool", "read_file", data)
    await drain(manager)
    before = attempts
    # A real new task grants a bounded burst, not an unbounded retry loop.
    next_data = {**data, "parent_task_id": "b" * 32, "task_label": "Later work"}
    event = "subagent.start"
    if new_work == "same_row":
        await manager.observe(source, "r", "s", 1, "subagent.complete", None, data)
        next_data = {**data, "attempt": 1, "resume_claim_id": "resume-1"}
        event = "subagent.admitted"
        # Failed validation and unclaimed admissions cannot grant recovery.
        for rejected in ({**next_data, "attempt": 2},
                         {**next_data, "thread_ref": "missing"},
                         {**next_data, "owner": {**data["owner"], "session_id": "other"}}):
            try:
                await manager.observe(source, "r", "s", 1, event, None, rejected)
            except ValueError:
                pass
            assert not card["reanchor"].get("new_work_pending")
        await manager.observe(source, "r", "s", 1, event, None, data)
        await drain(manager)
        assert attempts == before
    await manager.observe(source, "r", "s", 1, event, None, next_data)
    await drain(manager)
    assert attempts == before + (0 if failure == "ambiguous" else 3)
    exhausted = attempts
    await manager.observe(source, "r", "s", 1, event, None, next_data)
    await manager.observe(source, "r", "s", 1, "subagent.tool", "read_file", next_data)
    await drain(manager)
    assert attempts == exhausted
    if new_work == "same_row":
        assert card["rows"]["A"]["attempt"] == 1
        assert card["attempt_history"]["A"]["0"]["state"] in {"completed", "unknown"}
    adapter._bot.send_message.side_effect = original_send
    final_data = {**data, "parent_task_id": "c" * 32, "task_label": "Newest work"}
    await manager.observe(source, "r", "s", 1, "subagent.start", None, final_data)
    await drain(manager)
    persisted = json.loads(manager.path.read_text())
    assert len(live) == 1
    assert adapter._bot.delete_message.await_count == 1
    assert all(not c.get("handled") and not c.get("retired") for c in persisted.values())
    assert all(c["owner"] == data["owner"] for c in persisted.values())
    if failure == "ambiguous":
        assert card["reanchor"]["state"] == "sending"
        assert attempts == 1
    else:
        assert card["message_id"] in live
        labels = ["Check anchor", "Newest work"]
        if new_work == "start":
            labels.append("Later work")
        assert all(label in card["rendered"] for label in labels)
        assert persisted[data["parent_task_id"]]["message_id"] == card["message_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "ambiguous", "terminal"])
@pytest.mark.parametrize("new_work", ["start", "same_row"])
async def test_new_work_during_last_send_is_not_lost_or_allowed_to_bypass_fence(tmp_path, outcome, new_work):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    original_send = adapter._bot.send_message.side_effect
    attempts = 0
    next_data = {**data, "parent_task_id": "b" * 32, "task_label": "Arrived during send"}
    event = "subagent.start"
    if new_work == "same_row":
        next_data = {**data, "attempt": 1, "resume_claim_id": "inflight-resume"}
        event = "subagent.admitted"

    async def send(**kw):
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            if attempts == 3:
                if new_work == "same_row":
                    await manager.observe(source, "r", "s", 1, "subagent.complete", None, data)
                await manager.observe(source, "r", "s", 1, event, None, next_data)
                if outcome == "ambiguous":
                    await original_send(**kw)
                    raise TimedOut("accepted but lost receipt")
                if outcome == "terminal":
                    for task in (data, next_data):
                        await manager.observe(source, "r", "s", 1, "subagent.complete", None,
                                              {**task, "status": "completed"})
            raise Forbidden("definitely not sent")
        deadline = json.loads(manager.path.read_text())[data["parent_task_id"]]["reanchor"]["retry_not_before"]
        assert anchor.time.time() >= deadline
        return await original_send(**kw)

    adapter._bot.send_message.side_effect = send
    for mid in range(anchor.DISPLACEMENT):
        await inbound(adapter, 1000 + mid)
    await drain(manager)
    assert adapter._bot.delete_message.await_count == 1
    assert attempts == (4 if outcome == "rejected" else 3)
    assert len(live) == (0 if outcome == "terminal" else 1)
    if outcome == "rejected":
        assert next_data["task_label"] in card["rendered"]
    elif outcome == "ambiguous":
        assert card["reanchor"]["state"] == "sending"
    else:
        assert card["message_id"] is None
    # Reconciliation/restart alone never creates more sends or loses outcomes.
    restored = DelegationCards(manager.runner, home=tmp_path, interval=0)
    await restored.reconcile()
    await drain(restored)
    assert attempts == (4 if outcome == "rejected" else 3)
    assert all(not c.get("handled") and not c.get("retired") for c in restored.cards.values())
