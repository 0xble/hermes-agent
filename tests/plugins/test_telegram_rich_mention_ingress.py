"""Native PTB update routing for structured rich-message mentions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram import Update
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter

@pytest.mark.parametrize("node,accepted", [
    ({"type": "text_mention", "text": "Helpful assistant", "user": {"id": 999}}, True),
    ({"type": "bold", "text": {"type": "text_mention", "text": "Helper", "user": {"id": 999}}}, True),
    ([{"type": "mention", "username": "other_bot"}, {"type": "text_mention", "text": "Helper", "user": {"id": 999}}], True),
    ({"type": "text_mention", "text": "Someone", "user": {"id": 998}}, False),
    ({"type": "text_mention", "text": "Helper", "user": {"id": "999"}}, False),
    ({"type": "url", "text": "Helper", "url": "tg://user?id=999"}, False),
    ("[Helper](tg://user?id=999)", False),
])
@pytest.mark.parametrize("guest_mode", [False, True])
def test_rich_typed_user_mention_routes_through_group_ingress(node, accepted, guest_mode):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fixture", extra={}))
    adapter._is_user_authorized_from_message = MagicMock(return_value=True)
    adapter._should_observe_unmentioned_group_message = MagicMock(return_value=False)
    adapter._ensure_forum_commands = AsyncMock()
    adapter._cache_replied_media = AsyncMock()
    adapter._enqueue_text_event = MagicMock()
    adapter.config.extra.update({"require_mention": True, "exclusive_bot_mentions": True,
                                 "guest_mode": guest_mode})
    adapter._bot = SimpleNamespace(id=999, username="hermes_bot")
    update = Update.de_json({"update_id": 1200, "message": {
        "message_id": 80, "date": 0,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {"id": 42, "is_bot": False, "first_name": "Brian"},
        "rich_message": {"blocks": [{"type": "paragraph", "text": node}]},
    }}, bot=None)
    assert adapter._should_process_message(update.effective_message) is accepted
    asyncio.run(adapter._handle_rich_message(update, None))
    assert adapter._enqueue_text_event.call_count == int(accepted)


def test_rich_mention_identity_obeys_projection_visit_budget():
    from plugins.platforms.telegram.rich_messages import project_rich_message

    mention = {"type": "text_mention", "text": "Helper", "user": {"id": 999}}
    payload = {"blocks": [{"type": "paragraph", "text": ["prefix", mention]}]}
    assert project_rich_message(payload).mentioned_user_ids == (999,)
    for limits in ({"max_nodes": 2}, {"max_depth": 1}, {"max_chars": 3}):
        projection = project_rich_message(payload, **limits)
        assert projection.truncated
        assert projection.mentioned_user_ids == ()
