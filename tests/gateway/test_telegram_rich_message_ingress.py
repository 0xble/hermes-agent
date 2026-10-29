"""Inbound Telegram Bot API Rich Messages reach the agent instead of vanishing.

A formatted paste arrives as ``rich_message`` blocks with no ``text``. PTB 22.8 keeps the
unknown field only in ``Message.api_kwargs``, so ``filters.TEXT`` misses it and, before this
fix, no handler claimed the update: no inbound log, no reply. Invariants:

* a rich-only message enters the normal text pipeline once, as bounded Markdown;
* text, caption, and media messages keep their established handlers;
* authorization runs before projection; unaddressed group chatter is observed, not dispatched;
* Rich Message text participates in group mention gating;
* anything no handler claims is logged (field names only) rather than silently dropped.
"""

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
    m = MappingProxyType
    return m({"blocks": (
        m({"type": "heading", "size": 2, "text": "Launch plan"}),
        m({"type": "paragraph", "text": ("Use ", m({"type": "bold", "text": "care"}), " and ",
                                         m({"type": "code", "text": "wakeAgent"}))}),
        m({"type": "list", "items": (
            m({"label": "-", "has_checkbox": True, "is_checked": True,
               "blocks": (m({"type": "paragraph", "text": "First"}),)}),
            m({"label": "2.", "value": 2, "blocks": (m({"type": "paragraph", "text": "Second"}),)}),
        )}),
        m({"type": "details", "summary": "More", "blocks": (m({"type": "paragraph", "text": "Inside"}),)}),
        m({"type": "table", "cells": (
            (m({"text": "Name", "is_header": True}), m({"text": "Value", "is_header": True})),
            (m({"text": "A|B"}), m({"text": "1"})),
        )}),
        m({"type": "expandable_blockquote", "text": "Quote"}),
    )})


_EXPECTED_MARKDOWN = (
    "## Launch plan\n"
    "Use **care** and `wakeAgent`\n"
    "- [x] First\n"
    "2. Second\n"
    "**More**\n"
    "Inside\n"
    "| Name | Value |\n"
    "| --- | --- |\n"
    "| A\\|B | 1 |\n"
    "> Quote"
)


def _message(*, rich=None, text=None, caption=None, chat_type="private", **kwargs):
    defaults = dict(
        message_id=77,
        chat=SimpleNamespace(id=123, type=chat_type, title="Team" if chat_type != "private" else None,
                             full_name="Brian" if chat_type == "private" else None, is_forum=False),
        from_user=SimpleNamespace(id=42, full_name="Brian", username="brian", is_bot=False),
        sender_chat=None, business_connection_id=None, text=text, caption=caption,
        entities=[], caption_entities=[], message_thread_id=None, direct_messages_topic=None,
        is_topic_message=False, reply_to_message=None, quote=None, date=datetime.now(timezone.utc),
        forum_topic_created=None, photo=None, document=None, video=None, audio=None, voice=None,
        sticker=None, api_kwargs=MappingProxyType({"rich_message": rich} if rich is not None else {}),
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _update(message, update_id=990):
    return SimpleNamespace(update_id=update_id, message=message, edited_message=None,
                           effective_message=message)


def _adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={"allow_from": ["*"]}))
    adapter._is_user_authorized_from_message = MagicMock(return_value=True)
    adapter._should_process_message = MagicMock(return_value=True)
    adapter._should_observe_unmentioned_group_message = MagicMock(return_value=False)
    adapter._ensure_forum_commands = AsyncMock()
    adapter._cache_replied_media = AsyncMock()
    adapter._enqueue_text_event = MagicMock()
    adapter._observe_unmentioned_group_message = MagicMock()
    return adapter


def test_rich_handler_registered_after_media_and_before_final_unmatched_guard(monkeypatch):
    class FakeHandler:
        def __init__(self, message_filter, callback):
            self.callback = callback

    class App:
        def __init__(self):
            self.handlers = []

        def add_handler(self, handler, group=0):
            self.handlers.append((group, handler))

    monkeypatch.setattr(telegram_adapter, "TelegramMessageHandler", FakeHandler)
    app = App()
    _adapter()._register_handlers(app)
    callbacks = [h.callback.__name__ for g, h in app.handlers if g == 0 and isinstance(h, FakeHandler)]
    assert callbacks.index("_handle_media_message") < callbacks.index("_handle_rich_message")
    assert callbacks[-1] == "_handle_unmatched_message"


def test_real_ptb_rich_only_update_misses_text_filter_but_matches_rich_filter():
    telegram = pytest.importorskip("telegram")
    filters = pytest.importorskip("telegram.ext.filters")
    if not hasattr(telegram, "Update"):
        pytest.skip("real python-telegram-bot unavailable")
    update = telegram.Update.de_json({"update_id": 1, "message": {
        "message_id": 77, "date": 0, "chat": {"id": 123, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "Brian"},
        "rich_message": {"blocks": [{"type": "paragraph", "text": "Formatted body"}]},
    }}, bot=None)
    assert update.effective_message.text is None
    assert filters.TEXT.check_update(update) is False
    assert TelegramAdapter._rich_message_filter().check_update(update)


def test_rich_only_message_enters_text_pipeline_once_as_markdown():
    adapter = _adapter()
    msg = _message(rich=_rich_payload())
    asyncio.run(adapter._handle_rich_message(_update(msg, 991), None))
    adapter._enqueue_text_event.assert_called_once()
    event = adapter._enqueue_text_event.call_args.args[0]
    assert event.text == _EXPECTED_MARKDOWN
    assert event.platform_update_id == 991
    assert event.raw_message is msg
    meta = event.metadata["telegram_rich_message"]
    assert meta["source"] == "api_kwargs" and meta["truncated"] is False
    assert "table" in meta["block_types"]


def test_future_typed_rich_message_attribute_is_supported():
    adapter = _adapter()
    msg = _message()
    msg.rich_message = _rich_payload()
    asyncio.run(adapter._handle_rich_message(_update(msg, 992), None))
    event = adapter._enqueue_text_event.call_args.args[0]
    assert event.text.startswith("## Launch plan")
    assert event.metadata["telegram_rich_message"]["source"] == "attribute"


@pytest.mark.parametrize("extra", [
    {"text": "plain"}, {"caption": "caption"}, {"photo": [SimpleNamespace(file_id="p1")]},
])
def test_rich_filter_never_steals_text_caption_or_media(extra):
    assert TelegramAdapter._is_rich_message_update(_message(rich=_rich_payload())) is True
    assert TelegramAdapter._is_rich_message_update(_message(rich=_rich_payload(), **extra)) is False


def test_authorization_runs_before_projection():
    adapter = _adapter()
    adapter._is_user_authorized_from_message.return_value = False
    asyncio.run(adapter._handle_rich_message(_update(_message(rich=_rich_payload()), 993), None))
    adapter._should_process_message.assert_not_called()
    adapter._enqueue_text_event.assert_not_called()


def test_unaddressed_group_rich_message_is_observed_not_dispatched():
    adapter = _adapter()
    adapter._should_process_message.return_value = False
    adapter._should_observe_unmentioned_group_message.return_value = True
    asyncio.run(adapter._handle_rich_message(_update(_message(rich=_rich_payload(), chat_type="supergroup"), 994), None))
    adapter._enqueue_text_event.assert_not_called()
    adapter._observe_unmentioned_group_message.assert_called_once()
    assert adapter._observe_unmentioned_group_message.call_args.kwargs["event"].text == _EXPECTED_MARKDOWN


def test_rich_message_mentions_participate_in_group_gating():
    adapter = _adapter()
    adapter._bot = SimpleNamespace(username="hermes_bot", id=999)
    msg = _message(chat_type="supergroup", rich={"blocks": [{"type": "paragraph", "text": "@hermes_bot review this"}]})
    assert adapter._message_mentions_bot(msg) is True


def test_rich_reply_context_uses_markdown_projection():
    reply = _message(rich=_rich_payload())
    assert TelegramAdapter._extract_rich_reply_text(reply) == _EXPECTED_MARKDOWN
    assert TelegramAdapter._extract_rich_reply_text(_message(text="plain")) is None


def test_malformed_or_empty_payload_is_bounded_and_logged(caplog):
    adapter = _adapter()
    nested = {"type": "details", "summary": "root", "blocks": []}
    current = nested
    for _ in range(100):
        child = {"type": "details", "summary": "nested", "blocks": []}
        current["blocks"].append(child)
        current = child
    asyncio.run(adapter._handle_rich_message(_update(_message(rich={"blocks": [nested]}), 995), None))
    event = adapter._enqueue_text_event.call_args.args[0]
    assert len(event.text) <= 32768
    assert event.metadata["telegram_rich_message"]["truncated"] is True

    adapter._enqueue_text_event.reset_mock()
    with caplog.at_level(logging.WARNING):
        asyncio.run(adapter._handle_rich_message(_update(_message(rich={"blocks": []}), 996), None))
    assert "no readable content" in caplog.text
    adapter._enqueue_text_event.assert_not_called()


def test_unmatched_message_is_logged_with_field_names_only(caplog):
    adapter = _adapter()
    msg = _message(api_kwargs=MappingProxyType({"future_payload": {"secret": "do-not-log"}}))
    with caplog.at_level(logging.WARNING):
        asyncio.run(adapter._handle_unmatched_message(_update(msg, 997), None))
    assert "Unhandled Telegram message" in caplog.text
    assert "future_payload" in caplog.text
    assert "do-not-log" not in caplog.text


def test_projection_limits_hold():
    assert project_rich_message({"blocks": [{"type": "table", "cells": [
        "not-a-row", [{"text": "Name", "is_header": True}], [{"text": "Brian"}]]}]}).text == "| Name |\n| --- |\n| Brian |"
    tiny = project_rich_message({"blocks": [{"type": "paragraph", "text": "x" * 100}]}, max_chars=10)
    assert tiny.truncated is True and len(tiny.text) <= 10
