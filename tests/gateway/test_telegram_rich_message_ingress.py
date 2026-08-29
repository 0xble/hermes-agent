"""Regression coverage for inbound Telegram Bot API Rich Messages (HERMES-068)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.rich_messages import project_rich_message
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

telegram_adapter = load_plugin_adapter("telegram")
TelegramAdapter = telegram_adapter.TelegramAdapter


def _rich_payload():
    return MappingProxyType(
        {
            "blocks": (
                MappingProxyType(
                    {
                        "type": "heading",
                        "size": 2,
                        "text": "Launch plan",
                    }
                ),
                MappingProxyType(
                    {
                        "type": "paragraph",
                        "text": (
                            "Use ",
                            MappingProxyType({"type": "bold", "text": "care"}),
                        ),
                    }
                ),
                MappingProxyType(
                    {
                        "type": "list",
                        "items": (
                            MappingProxyType(
                                {
                                    "label": "-",
                                    "has_checkbox": True,
                                    "is_checked": True,
                                    "blocks": (
                                        MappingProxyType(
                                            {"type": "paragraph", "text": "First"}
                                        ),
                                    ),
                                }
                            ),
                            MappingProxyType(
                                {
                                    "label": "2.",
                                    "value": 2,
                                    "blocks": (
                                        MappingProxyType(
                                            {"type": "paragraph", "text": "Second"}
                                        ),
                                    ),
                                }
                            ),
                        ),
                    }
                ),
                MappingProxyType(
                    {
                        "type": "details",
                        "summary": "More",
                        "blocks": (
                            MappingProxyType(
                                {"type": "paragraph", "text": "Inside"}
                            ),
                        ),
                    }
                ),
                MappingProxyType(
                    {
                        "type": "table",
                        "cells": (
                            (
                                MappingProxyType(
                                    {"text": "Name", "is_header": True}
                                ),
                                MappingProxyType(
                                    {"text": "Value", "is_header": True}
                                ),
                            ),
                            (
                                MappingProxyType({"text": "A|B"}),
                                MappingProxyType({"text": "1"}),
                            ),
                        ),
                    }
                ),
                MappingProxyType(
                    {
                        "type": "expandable_blockquote",
                        "text": "Quote",
                    }
                ),
                MappingProxyType(
                    {
                        "type": "paragraph",
                        "text": MappingProxyType(
                            {
                                "type": "bot_command",
                                "text": "/reasoning high",
                                "bot_command": "/reasoning",
                            }
                        ),
                    }
                ),
            )
        }
    )


def _message(*, rich=None, text=None, caption=None, chat_type="private", **kwargs):
    defaults = dict(
        message_id=77,
        chat=SimpleNamespace(
            id=123,
            type=chat_type,
            title="Launch team" if chat_type != "private" else None,
            full_name="Brian" if chat_type == "private" else None,
            is_forum=False,
        ),
        from_user=SimpleNamespace(id=42, full_name="Brian", username="brian"),
        sender_chat=None,
        business_connection_id=None,
        text=text,
        caption=caption,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        direct_messages_topic=None,
        is_topic_message=False,
        reply_to_message=None,
        quote=None,
        date=datetime.now(timezone.utc),
        forum_topic_created=None,
        photo=None,
        document=None,
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        api_kwargs=MappingProxyType(
            {"rich_message": rich} if rich is not None else {}
        ),
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _update(message, update_id=990):
    return SimpleNamespace(
        update_id=update_id,
        message=message,
        edited_message=None,
        edited_channel_post=None,
        business_message=None,
        edited_business_message=None,
        effective_message=message,
    )


def _adapter():
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={"allow_from": ["*"]})
    )
    adapter._is_user_authorized_from_message = MagicMock(return_value=True)
    adapter._should_process_message = MagicMock(return_value=True)
    adapter._should_observe_unmentioned_group_message = MagicMock(return_value=False)
    adapter._ensure_forum_commands = AsyncMock()
    adapter._cache_replied_media = AsyncMock()
    adapter._enqueue_text_event = MagicMock()
    adapter._observe_unmentioned_group_message = MagicMock()
    return adapter


def test_registration_routes_rich_message_before_final_unmatched_guard(monkeypatch):
    class FakeHandler:
        def __init__(self, message_filter, callback):
            self.message_filter = message_filter
            self.callback = callback

    class App:
        def __init__(self):
            self.handlers = []

        def add_handler(self, handler, group=0):
            self.handlers.append((group, handler))

    monkeypatch.setattr(telegram_adapter, "TelegramMessageHandler", FakeHandler)
    adapter = _adapter()
    app = App()

    adapter._register_handlers(app)

    callbacks = [
        handler.callback.__name__
        for group, handler in app.handlers
        if group == 0 and isinstance(handler, FakeHandler)
    ]
    assert callbacks.index("_handle_media_message") < callbacks.index(
        "_handle_rich_message"
    )
    assert callbacks.index("_handle_rich_message") < callbacks.index(
        "_handle_unmatched_message"
    )
    assert callbacks[-1] == "_handle_unmatched_message"


def test_real_ptb_rich_only_update_rejects_text_filter_but_matches_adapter():
    telegram = pytest.importorskip("telegram")
    filters = pytest.importorskip("telegram.ext.filters")
    if not hasattr(telegram, "Update"):
        pytest.skip("real python-telegram-bot unavailable")

    update = telegram.Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 77,
                "date": 0,
                "chat": {"id": 123, "type": "private"},
                "from": {
                    "id": 42,
                    "is_bot": False,
                    "first_name": "Brian",
                },
                "rich_message": {
                    "blocks": [
                        {"type": "paragraph", "text": "Formatted body"}
                    ]
                },
            },
        },
        bot=None,
    )

    message = update.effective_message
    assert message.text is None
    assert filters.TEXT.check_update(update) is False
    assert TelegramAdapter._is_rich_message_update(message) is True


def test_mapping_like_rich_message_enters_normal_text_pipeline_once():
    adapter = _adapter()
    msg = _message(rich=_rich_payload())

    asyncio.run(adapter._handle_rich_message(_update(msg, 991), None))

    adapter._enqueue_text_event.assert_called_once()
    event = adapter._enqueue_text_event.call_args.args[0]
    assert event.text == (
        "## Launch plan\n"
        "Use **care**\n"
        "- [x] First\n"
        "2. Second\n"
        "**More**\n"
        "Inside\n"
        "| Name | Value |\n"
        "| --- | --- |\n"
        "| A\\|B | 1 |\n"
        "> Quote\n"
        "/reasoning high"
    )
    assert event.platform_update_id == 991
    assert event.raw_message is msg
    assert msg.text is None
    assert event.metadata["telegram_rich_message"] == {
        "block_count": 10,
        "block_types": [
            "heading",
            "paragraph",
            "list",
            "details",
            "table",
            "expandable_blockquote",
        ],
        "source": "api_kwargs",
        "truncated": False,
    }


def test_future_native_rich_message_attribute_is_supported():
    adapter = _adapter()
    msg = _message(rich=None)
    msg.rich_message = _rich_payload()

    asyncio.run(adapter._handle_rich_message(_update(msg, 992), None))

    event = adapter._enqueue_text_event.call_args.args[0]
    assert event.text.startswith("## Launch plan")
    assert event.metadata["telegram_rich_message"]["source"] == "attribute"


def test_rich_filter_does_not_steal_text_caption_or_media_messages():
    rich = _rich_payload()
    assert TelegramAdapter._is_rich_message_update(_message(rich=rich)) is True
    assert (
        TelegramAdapter._is_rich_message_update(_message(rich=rich, text="plain"))
        is False
    )
    assert (
        TelegramAdapter._is_rich_message_update(
            _message(rich=rich, caption="caption")
        )
        is False
    )
    assert (
        TelegramAdapter._is_rich_message_update(
            _message(rich=rich, photo=[SimpleNamespace(file_id="p1")])
        )
        is False
    )


def test_rich_message_respects_authorization_before_projection():
    adapter = _adapter()
    adapter._is_user_authorized_from_message.return_value = False
    msg = _message(rich=_rich_payload())

    asyncio.run(adapter._handle_rich_message(_update(msg, 993), None))

    adapter._should_process_message.assert_not_called()
    adapter._enqueue_text_event.assert_not_called()


def test_untriggered_group_rich_message_is_observed_with_text_and_metadata():
    adapter = _adapter()
    adapter._should_process_message.return_value = False
    adapter._should_observe_unmentioned_group_message.return_value = True
    transcript_entries = []
    adapter._session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        append_to_transcript=lambda session_id, entry: transcript_entries.append(entry),
    )
    adapter._observe_unmentioned_group_message = (
        TelegramAdapter._observe_unmentioned_group_message.__get__(
            adapter, TelegramAdapter
        )
    )
    msg = _message(rich=_rich_payload(), chat_type="supergroup")

    asyncio.run(adapter._handle_rich_message(_update(msg, 994), None))

    adapter._enqueue_text_event.assert_not_called()
    assert len(transcript_entries) == 1
    entry = transcript_entries[0]
    assert entry["content"].endswith("/reasoning high")
    assert entry["metadata"]["telegram_rich_message"]["block_count"] == 10
    assert entry["metadata"]["telegram_rich_message"]["source"] == "api_kwargs"


def test_rich_message_mentions_participate_in_group_gating():
    adapter = _adapter()
    adapter._bot = SimpleNamespace(username="hermes_bot", id=999)
    msg = _message(
        chat_type="supergroup",
        rich=MappingProxyType(
            {
                "blocks": (
                    MappingProxyType(
                        {
                            "type": "paragraph",
                            "text": "@hermes_bot review this",
                        }
                    ),
                )
            }
        ),
    )

    assert adapter._message_mentions_bot(msg) is True


def test_malformed_or_empty_rich_payload_is_bounded_and_observable(caplog):
    adapter = _adapter()
    nested = {"type": "details", "summary": "root", "blocks": []}
    current = nested
    for _ in range(100):
        child = {"type": "details", "summary": "nested", "blocks": []}
        current["blocks"].append(child)
        current = child
    msg = _message(rich={"blocks": [nested]})

    asyncio.run(adapter._handle_rich_message(_update(msg, 995), None))

    event = adapter._enqueue_text_event.call_args.args[0]
    assert len(event.text) <= 32768
    assert event.metadata["telegram_rich_message"]["truncated"] is True

    empty = _message(rich={"blocks": []})
    adapter._enqueue_text_event.reset_mock()
    with caplog.at_level(logging.WARNING):
        asyncio.run(adapter._handle_rich_message(_update(empty, 996), None))
    assert "no readable content" in caplog.text
    adapter._enqueue_text_event.assert_not_called()


def test_table_header_index_ignores_malformed_rows():
    projection = project_rich_message(
        {
            "blocks": [
                {
                    "type": "table",
                    "cells": [
                        "not-a-row",
                        [{"text": "Name", "is_header": True}],
                        [{"text": "Brian"}],
                    ],
                }
            ]
        }
    )

    assert projection.text == "| Name |\n| --- |\n| Brian |"


def test_tiny_character_limit_remains_hard_bounded():
    projection = project_rich_message(
        {"blocks": [{"type": "paragraph", "text": "x" * 100}]},
        max_chars=10,
    )

    assert projection.truncated is True
    assert len(projection.text) <= 10
