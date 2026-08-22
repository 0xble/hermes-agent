from __future__ import annotations

import pytest

from plugins.platforms.telegram.topic_icon_runtime import ProfileTopicIconService
from plugins.platforms.telegram.topic_icons import CustomTopicIconCatalog
from plugins.platforms.telegram.user_transport import TelegramUserIdentity


class _Bot:
    async def get_me(self):
        return type("BotIdentity", (), {"id": 202, "username": "hermes_bot"})()

    async def get_dialogs(self):
        raise AssertionError("peer status must never enumerate dialogs")


class _Transport:
    async def identity(self):
        return TelegramUserIdentity(101, "tester", True)

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_runtime_peer_status_verifies_only_configured_bot_ids():
    runtime = ProfileTopicIconService(
        config={
            "enabled": True,
            "expected_user_id": 101,
            "capabilities": ["topic.read"],
            "allowed_bot_peer_ids": [202],
        },
        packs=("AppleObjectsHomeTools",),
        bot=_Bot(),
        transport=_Transport(),
        catalog=CustomTopicIconCatalog(("AppleObjectsHomeTools",)),
    )

    result = await runtime.peers()

    assert result["peers"] == [
        {"peer_id": 202, "verified": True, "username": "hermes_bot"}
    ]
