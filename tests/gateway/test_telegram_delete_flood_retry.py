"""The fork reports flood-refused deletes as unperformed so durable cleanup owns retry.

Upstream's accepted-background-delete contract would evict the status owner before
the deletion happens. The fork keeps that owner and performs no request or task
inside the shared flood window.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", [1.0, 240.0])
async def test_flood_refused_delete_keeps_owner_until_actual_retry(wait):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fixture"))
    adapter._bot = MagicMock()
    adapter._bot.delete_message = AsyncMock(return_value=True)
    key = ("4242", "", "progress")
    adapter._status_message_ids[key] = "55"
    adapter._record_send_flood_cooldown("4242", wait)
    tasks = set(adapter._background_tasks)
    assert await adapter.delete_message("4242", "55") is False
    adapter._bot.delete_message.assert_not_awaited()
    assert adapter._status_message_ids[key] == "55"
    assert set(adapter._background_tasks) == tasks
    adapter.__dict__["_telegram_send_cooldown_until"].clear()
    adapter.__dict__["_telegram_platform_flood_until"].clear()
    assert await adapter.delete_message("4242", "55") is True
    adapter._bot.delete_message.assert_awaited_once_with(chat_id=4242, message_id=55)
    assert key not in adapter._status_message_ids
