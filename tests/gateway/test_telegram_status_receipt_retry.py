"""Status receipts survive failed edits unless Telegram proves the anchor absent."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,replace", [
    (RetryAfter(60), False),
    (NetworkError("connection error"), False),
    (RuntimeError("unknown transport outcome"), False),
    (Forbidden("message can't be edited"), False),
    (BadRequest("Message to edit not found"), True),
    (BadRequest("MESSAGE_ID_INVALID"), True),
])
async def test_status_retries_known_receipt_until_definite_absence(failure, replace):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake", extra={"rich_messages": False}))
    adapter._send_cooldown_seconds = 0
    adapter._edit_min_interval_seconds = 0
    adapter._bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=[SimpleNamespace(message_id=100), SimpleNamespace(message_id=200)]),
        edit_message_text=AsyncMock(side_effect=failure),
    )
    adapter._retrigger_typing = AsyncMock()
    metadata = {"thread_id": "8", "business_connection_id": "business-fixture"}
    first = await adapter.send_or_update_status("42", "lifecycle", "starting", metadata=metadata)
    assert first.success and first.message_id == "100"

    failed = await adapter.send_or_update_status("42", "lifecycle", "working", metadata=metadata)
    assert failed.success is replace
    assert adapter._bot.send_message.await_count == (2 if replace else 1)
    assert list(adapter._status_message_ids.values()) == ["200" if replace else "100"]
    if not replace:
        # A subsequent explicit caller retry edits the same acknowledged receipt.
        adapter._send_cooldown_until.clear()
        adapter._send_penalty_until.clear()
        adapter._bot.edit_message_text.side_effect = None
        result = await adapter.send_or_update_status("42", "lifecycle", "done", metadata=metadata)
        assert result.success and result.message_id == "100"
        assert adapter._bot.edit_message_text.call_args.kwargs["message_id"] == 100
        assert adapter._bot.send_message.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error,retryable", [
    (None, False),
    ("Message to edit not found", True),
    ("flood_control: retry after 60", True),
])
async def test_status_preserves_unclassified_or_retryable_edit_receipt(error, retryable):
    from gateway.platforms.base import SendResult

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake"))
    key = ("42", "lifecycle", "8", "")
    adapter._status_message_ids[key] = "100"
    receipt = SendResult(success=False, error=error, retryable=retryable)
    adapter.edit_message = AsyncMock(return_value=receipt)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="200"))
    assert await adapter._send_locked_status(key, "42", "working", {"thread_id": "8"}) is receipt
    assert adapter._status_message_ids[key] == "100"
    adapter.send.assert_not_awaited()
