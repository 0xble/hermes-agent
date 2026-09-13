"""Reconnect control must not consume the internal no-request deletion receipt."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["replacement", "replacement_after_wait", "local_reconnect"])
@pytest.mark.parametrize("outcome", ["deferred", "failed", "deleted", "already_absent"])
async def test_status_delete_preserves_transport_outcome_across_reconnect(route, outcome):
    old = TelegramAdapter(PlatformConfig(enabled=True, token="fake"))
    live = old if route == "local_reconnect" else TelegramAdapter(PlatformConfig(enabled=True, token="fake"))
    live._send_cooldown_seconds = 0
    bot = SimpleNamespace(delete_message=AsyncMock(return_value=True))
    if outcome == "deferred":
        live._send_cooldown_until["42"] = time.monotonic() + 3600
    elif outcome == "failed":
        bot.delete_message.side_effect = Forbidden("cannot delete")
    elif outcome == "already_absent":
        bot.delete_message.side_effect = BadRequest("Message to delete not found")
    live._bot = bot
    old._bot = None
    old.gateway_runner = SimpleNamespace(adapters={})

    async def reconnect():
        if route == "local_reconnect":
            old._bot = bot
        else:
            old.gateway_runner.adapters[old.platform] = live
        return True

    old._wait_for_reconnection = AsyncMock(side_effect=reconnect)
    if route == "replacement":
        old.gateway_runner.adapters[old.platform] = live

    expected = None if outcome == "deferred" else outcome != "failed"
    assert await old._delete_status_message("42", "100") is expected
    if route == "replacement":
        old._wait_for_reconnection.assert_not_awaited()
    else:
        old._wait_for_reconnection.assert_awaited_once()
    assert bot.delete_message.await_count == (0 if outcome == "deferred" else 1)
    if outcome == "deferred":
        assert await old.delete_message("42", "100") is False  # public API stays bool
        bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_reanchor_refunds_replacement_adapter_deletion_deferral(tmp_path):
    from gateway import delegation_card_anchor as anchor
    from tests.gateway.test_delegation_card_anchor import fixture

    manager, old, source, data, card, physical, calls = await fixture(tmp_path)
    live = TelegramAdapter(PlatformConfig(enabled=True, token="fake"))
    live._bot = old._bot
    live._send_cooldown_until[source.chat_id] = time.monotonic() + 3600
    old._bot = None
    old.gateway_runner = SimpleNamespace(adapters={old.platform: live})
    calls.clear()
    original = card["message_id"]
    card["reanchor"] = dict(order="delete_first", state="delete_pending",
                            old_message_id=original, delete_attempts=0, send_attempts=0)
    manager._save()

    await anchor.replace(manager, data["parent_task_id"])

    assert card["reanchor"]["delete_attempts"] == 0
    assert card["reanchor"]["state"] == "delete_pending"
    assert card["message_id"] == original and original in physical
    assert calls == []
    persisted = json.loads(manager.path.read_text())
    assert persisted[data["parent_task_id"]]["reanchor"]["delete_attempts"] == 0
