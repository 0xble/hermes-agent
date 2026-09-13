"""Authored CommonMark spans serialize like real PTB CODE entities, offline."""
from datetime import datetime, timezone

import pytest
from telegram import Chat, Message, MessageEntity

from plugins.platforms.telegram.adapter import TelegramAdapter


def code_markdown(body):
    message = Message(message_id=1, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                      chat=Chat(1, "private"), text=body,
                      entities=[MessageEntity(type="code", offset=0,
                                              length=len(body.encode("utf-16-le")) // 2)])
    return message.text_markdown_v2


@pytest.mark.parametrize("ticks", ["``", "```", "````"])
@pytest.mark.parametrize("authored_body,body", [
    ("foo ` bar", "foo ` bar"),
    (r"a\b\`c [3](https://example.com/source)", r"a\b\`c [3](https://example.com/source)"),
    (" `edge` ", "`edge`"),
    ("one\ntwo", "one two"),
    ("   ", "   "),
])
def test_multibacktick_span_matches_sdk_code_serialization(ticks, authored_body, body):
    adapter = object.__new__(TelegramAdapter)
    # Inline context disambiguates even three ticks from an actual fenced block.
    assert adapter.format_message("before " + ticks + authored_body + ticks + " after") == (
        "before " + code_markdown(body) + " after")
    longer_run_body = "one " + ticks + "` two"
    assert adapter.format_message(ticks + longer_run_body + ticks) == code_markdown(longer_run_body)


@pytest.mark.parametrize("indent", ["", "  "])
def test_single_span_and_fenced_code_keep_existing_controls(indent):
    adapter = object.__new__(TelegramAdapter)
    body = r"a\b [3](https://example.com/source)"
    assert adapter.format_message("`" + body + "`") == code_markdown(body)
    fenced_body = "echo `host` \\path\n"
    expected = fenced_body.replace("\\", "\\\\").replace("`", "\\`")
    assert adapter.format_message(indent + "```sh\n" + fenced_body + indent + "```") == (
        indent + "```sh\n" + expected + indent + "```")
