"""Real card manager and Telegram gate: expiry never publishes a stale snapshot."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, RetryAfter

from gateway.config import Platform, PlatformConfig
from gateway.delegation_cards import DelegationCards
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter


async def drain(manager):
    for _ in range(20):
        if not manager.pending:
            return
        await asyncio.gather(*list(manager.pending.values()))
    raise AssertionError("unbounded card scheduling")


async def transport_started(manager):
    for _ in range(30):
        if manager._inflight:
            return
        await asyncio.sleep(0)
    raise AssertionError("card never reached transport")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["initial", "edit", "delete_queued", "delete_dispatched"])
async def test_expiry_and_resume_are_fresh_at_real_transport_gate(tmp_path, phase):
    now = [100.0]
    (tmp_path / "config.yaml").write_text("display:\n  delegation_terminal_ttl_seconds: 10\n")
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake", extra={"rich_messages": False}))
    adapter._send_cooldown_seconds = 0
    adapter._edit_min_interval_seconds = 0
    live, calls = {}, []
    dispatched, complete_delete = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        mid = str(100 + len(calls))
        calls.append(("send", mid))
        live[mid] = kwargs["text"]
        return SimpleNamespace(message_id=int(mid))

    async def edit(**kwargs):
        mid = str(kwargs["message_id"])
        calls.append(("edit", mid))
        live[mid] = kwargs["text"]
        return True

    async def delete(**kwargs):
        mid = str(kwargs["message_id"])
        calls.append(("delete", mid))
        if phase == "delete_dispatched":
            dispatched.set()
            await complete_delete.wait()
        live.pop(mid, None)
        return True

    adapter._bot = SimpleNamespace(send_message=AsyncMock(side_effect=send),
        edit_message_text=AsyncMock(side_effect=edit), delete_message=AsyncMock(side_effect=delete))
    manager = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter),
                              home=tmp_path, interval=0, clock=lambda: now[0])
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    key = "a" * 32
    data = dict(parent_task_id=key, thread_ref="A", task_label="Review candidate", owner=dict(
        profile="default", session_id="s", session_key="r", chat_id="42", thread_id="8"))
    gate = adapter._send_cooldown_lock("42")
    assert gate is not None
    if phase == "initial":
        await gate.acquire()
    await manager.observe(source, "r", "s", 1, "subagent.start", None, data)
    if phase == "initial":
        await transport_started(manager)
    else:
        await drain(manager)
        if phase == "edit":
            await gate.acquire()
    await manager.observe(source, "r", "s", 1, "subagent.complete", None, data)
    card = manager.cards[key]
    assert card["rows"]["A"]["display_expires_at"] == 110
    if phase == "edit":
        await transport_started(manager)
    elif phase.startswith("delete"):
        await drain(manager)
        if phase == "delete_queued":
            await gate.acquire()
    now[0] = 110
    scope = manager._scope(card)
    manager._expiry_callback(scope, key, manager._expiry_tokens[scope])
    if phase == "initial":
        gate.release()
        await drain(manager)
        assert calls == [] and card["message_id"] is None and card["send_attempts"] == 0
    elif phase == "delete_dispatched":
        await asyncio.wait_for(dispatched.wait(), timeout=5)
    elif phase == "delete_queued":
        await transport_started(manager)
    # Admission must not wait behind a queued edit or delete.
    await asyncio.wait_for(manager.observe(source, "r", "s", 2, "subagent.admitted", None,
        {**data, "attempt": 1, "resume_claim_id": "resume"}), timeout=2)
    assert "terminal_at" not in card["rows"]["A"]
    if phase == "delete_dispatched":
        complete_delete.set()
    elif phase != "initial":
        gate.release()
    await drain(manager)
    assert len(live) == 1 and card["message_id"] in live
    assert live[card["message_id"]] == "○ Review candidate"
    assert sum(kind == "delete" for kind, _ in calls) == (phase == "delete_dispatched")
    assert not card.get("handled") and not card.get("retired")
    assert card["attempt_history"]["A"]["0"]["terminal_at"] == 100
    await manager.observe(source, "r", "s", 2, "subagent.complete", None, {**data, "attempt": 1})
    assert card["rows"]["A"]["terminal_at"] == 110
    assert card["rows"]["A"]["display_expires_at"] == 120
    await drain(manager)
    await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["format", "flood", "oversize", "stale_delete"])
async def test_card_transport_preserves_shared_budget_and_exact_payload(outcome):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake", extra={"rich_messages": False}))
    adapter._send_cooldown_seconds = 0
    adapter._edit_min_interval_seconds = 0
    adapter._bot = SimpleNamespace(edit_message_text=AsyncMock(return_value=True), delete_message=AsyncMock(return_value=True))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    contents = iter(["First payload", "Updated payload"])
    if outcome == "stale_delete":
        assert await adapter._delete_status_message("42", "100", guard=lambda: False) is None
        adapter._bot.delete_message.assert_not_awaited()
        return
    if outcome == "format":
        adapter._bot.edit_message_text.side_effect = [BadRequest("can't parse entities"), True]
    if outcome == "flood":
        adapter._bot.edit_message_text.side_effect = RetryAfter(3600)
    supplier = (lambda: "x" * 5000) if outcome == "oversize" else lambda: next(contents)
    result = await adapter.edit_delegation_card(source, "100", supplier)
    if outcome == "format":
        assert result.success
        assert adapter._bot.edit_message_text.await_args.kwargs["text"] == "Updated payload"
    elif outcome == "flood":
        assert not result.success and result.retryable and result.retry_after >= 3599
        assert await adapter._delete_status_message("42", "100") is None
        adapter._bot.delete_message.assert_not_awaited()
    else:
        assert not result.success and "limit" in result.error
        adapter._bot.edit_message_text.assert_not_awaited()
