"""Fresh TTL-aware edits retain the same Telegram business route as card sends."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter
from telegram.error import BadRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_fresh_card_edit_preserves_business_route_on_every_attempt(fallback):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fixture"))
    adapter._bot = MagicMock()
    adapter._bot.edit_message_text = AsyncMock(
        side_effect=[BadRequest("can't parse entities"), SimpleNamespace(message_id=7)]
        if fallback else None)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42",
                           business_connection_id="fixture-business")
    payloads = iter(["○ First task", "○ Latest task"])
    result = await adapter.edit_delegation_card(source, "7", lambda: next(payloads))
    assert result.success and result.message_id == "7"
    calls = adapter._bot.edit_message_text.await_args_list
    assert len(calls) == (2 if fallback else 1)
    assert all(call.kwargs.get("business_connection_id") == "fixture-business" for call in calls)
    assert all(call.kwargs["chat_id"] == 42 and call.kwargs["message_id"] == 7 for call in calls)
    assert "Latest task" in calls[-1].kwargs["text"] if fallback else "First task" in calls[-1].kwargs["text"]
