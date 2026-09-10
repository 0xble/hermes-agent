"""Assistant-only displacement through real Telegram send/edit/media entry points."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import delegation_card_anchor as anchor
from gateway.platforms.base import SendResult
from tests.gateway.test_delegation_card_anchor import fixture, drain, inbound


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["plain", "rich", "overflow", "media", "mixed"])
async def test_assistant_only_and_mixed_traffic_reanchor_once(tmp_path, lane):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    initial = card["message_id"]
    adapter._retrigger_typing = AsyncMock()
    metadata = {"thread_id": "8", "notify": True}
    if lane == "rich":
        adapter._should_attempt_rich = lambda *a, **kw: True
        adapter._try_send_rich = AsyncMock(side_effect=[SendResult(success=True, message_id=str(i)) for i in range(200, 200 + anchor.DISPLACEMENT)])
    for i in range(anchor.DISPLACEMENT):
        if lane == "overflow":
            await adapter._send_overflow_continuation("42", "New physical continuation", None,
                {"message_thread_id": 8}, "8", metadata, True)
        elif lane == "media":
            await adapter._send_media(adapter._bot.send_message, "42", None, metadata, "test payload", text="test")
        elif lane == "mixed" and i == 1:
            await inbound(adapter, 900)
        else:
            await adapter.send("42", "Assistant commentary or final", metadata=metadata)
        # A duplicate update of an already observed physical outbound ID is inert.
        if lane == "rich":
            await inbound(adapter, 200 + i)
        if i < anchor.DISPLACEMENT - 1:
            await drain(manager)
            assert card["message_id"] == initial
    await drain(manager)
    replacement = card["message_id"]
    assert replacement != initial
    assert card["last_reanchor"]["old_message_id"] == initial
    # Card itself never feeds the observer; ordinary edits do not count either.
    before = set(manager.displacement.get(data["parent_task_id"], ()))
    await adapter.edit_message("42", replacement, "Updated card", metadata={**metadata, "hermes_status": True})
    await drain(manager)
    assert set(manager.displacement.get(data["parent_task_id"], ())) == before
    assert card["message_id"] == replacement


@pytest.mark.asyncio
async def test_failed_sends_statuses_and_wrong_topic_never_displace(tmp_path):
    manager, adapter, source, data, card, live, calls = await fixture(tmp_path)
    adapter._retrigger_typing = AsyncMock()
    for _ in range(3):
        await adapter.send("42", "Status", metadata={"thread_id": "8", "hermes_status": True})
        await adapter.send("42", "Other topic", metadata={"thread_id": "9"})
    adapter._send_impl = AsyncMock(return_value=SendResult(success=False))
    await adapter.send("42", "Failed reply", metadata={"thread_id": "8"})
    await drain(manager)
    assert not manager.displacement.get(data["parent_task_id"])
