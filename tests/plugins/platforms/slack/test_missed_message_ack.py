from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


@pytest.mark.asyncio
async def test_fetch_missed_inbound_builds_events_and_filters_history():
    client = AsyncMock()
    client.users_conversations.return_value = {
        "channels": [
            {"id": "D1", "is_im": True},
            {"id": "C1", "is_channel": True, "is_member": True},
        ],
    }
    histories = {
        "D1": {"messages": [
            {"user": "U1", "ts": "1500.1", "text": "missed dm"},
            {"user": "B1", "ts": "1500.2", "text": "own bot message"},
            {"bot_id": "APP", "ts": "1500.3", "text": "workflow bot"},
        ]},
        "C1": {"messages": [
            {"user": "U1", "ts": "1501.1", "text": "<@B1> missed mention"},
            {"user": "U1", "ts": "1501.2", "text": "not addressed"},
            {"user": "U1", "ts": "1501.3", "text": "<@B1> already answered", "reply_count": 1},
        ]},
    }
    client.conversations_history.side_effect = lambda **kwargs: histories[kwargs["channel"]]
    client.conversations_replies.return_value = {"messages": [
        {"user": "U1", "ts": "1501.3", "text": "<@B1> already answered"},
        {"user": "B1", "ts": "1501.4", "text": "answer"},
    ]}

    adapter = SlackAdapter(PlatformConfig(enabled=True))
    adapter._app = SimpleNamespace(client=client)
    adapter._team_clients = {"T1": client}
    adapter._team_bot_user_ids = {"T1": "B1"}
    adapter._bot_user_id = "B1"
    adapter._authorization_check = None

    records = await adapter.fetch_missed_inbound(1000, 2000)

    assert [(record["conversation"], record["ts"]) for record in records] == [
        ("D1", "1500.1"), ("C1", "1501.1"),
    ]
    assert records[0]["event"]["text"] == "missed dm"
    client.users_conversations.assert_awaited_once()
    assert client.conversations_history.await_count == 2
    client.conversations_replies.assert_awaited_once_with(
        channel="C1", ts="1501.3", limit=1000, inclusive=True,
    )


