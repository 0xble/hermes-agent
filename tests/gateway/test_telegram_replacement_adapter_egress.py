"""Calls on a reconnect-replaced Telegram adapter reach the live one.

When polling recovery rebuilds the adapter, the gateway publishes a new instance in
``runner.adapters``. A turn already in flight still holds the retired instance, whose
``_bot`` is gone. ``send()`` already forwarded to the live adapter, but edits, deletes,
and typing did not. A failed edit was read as permanent, so tool progress for the rest
of the turn arrived as one new message per line.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _retired_and_live():
    retired = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    retired._bot = None
    live = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    live._bot = MagicMock()
    runner = SimpleNamespace(adapters={retired.platform: live})
    retired.gateway_runner = runner
    live.gateway_runner = runner
    return retired, live


@pytest.mark.asyncio
async def test_edit_on_retired_adapter_lands_on_the_live_one():
    retired, live = _retired_and_live()
    live._bot.edit_message_text = AsyncMock()

    result = await retired.edit_message("4242", "17", "progress line")

    assert result.success is True
    assert live._bot.edit_message_text.await_count == 1
    assert live._bot.edit_message_text.await_args.kwargs["message_id"] == 17


@pytest.mark.asyncio
async def test_delete_on_retired_adapter_lands_on_the_live_one():
    retired, live = _retired_and_live()
    live._bot.delete_message = AsyncMock()

    assert await retired.delete_message("4242", "17") is True
    live._bot.delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_retired_adapter_without_a_live_one_still_refuses_edits():
    retired = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    retired._bot = None

    result = await retired.edit_message("4242", "17", "progress line")

    assert result.success is False
    assert result.error == "Not connected"
    # Transient: the progress loop keeps its bubble and retries instead of going one-message-per-line.
    assert result.retryable is True
