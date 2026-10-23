"""Opening briefs stay compact through the real Rich Message shaping path.

The relay envelope's title + objective + ``---`` was a multiline setext
heading. Paragraph-spacer normalization pulled the origin into it as well.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from markdown_it import MarkdownIt

from gateway.config import Platform, PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.session import SessionSource
from gateway.telegram_topic_sessions import _deliver_brief


BRIEF = (
    "📨 Teach humor via On Jokes\n"
    "Walk through the book chapter by chapter and practice real conversation.\n"
    "---\n"
    "Owns: humor lessons, with *On Jokes* (Jimmy Carr, Amanda Baker and Abi Grant).\n\n"
    'Why: the digest was unhelpful. Original request: "teach me to be funny".\n\n'
    "Next action: read the whole book first. The EPUB is at `~/Library/books/On Jokes.epub`. "
    "Wait between chapters.\n\n"
    "Limits: message no one else. No runtime restart.\n\n"
    "Full handoff: `~/handoff.md`. Source topic: 1234."
)
ORIGIN = "Split from [Calling Topic](https://t.me/example_bot/1234/1234)"


def _adapter(mode: bool | str = "always"):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={"rich_messages": mode}))
    bot = MagicMock()
    bot.do_api_request = AsyncMock(return_value=SimpleNamespace(message_id=77))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=77))
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    return adapter


def _visible(markdown):
    return markdown.split("<details>", 1)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [True, "always"])
async def test_brief_keeps_title_objective_and_next_outside_details(monkeypatch, mode):
    adapter = _adapter(mode)
    admit = AsyncMock()
    monkeypatch.setattr("gateway.wake.admit_internal_event", admit)
    dest = SessionSource(platform=Platform.TELEGRAM, chat_id="123", thread_id="456")

    await _deliver_brief(adapter, dest, BRIEF, ORIGIN)

    call = adapter._bot.do_api_request.call_args
    assert call.args[0] == "sendRichMessage"
    assert call.kwargs["api_kwargs"]["message_thread_id"] == 456
    markdown = call.kwargs["api_kwargs"]["rich_message"]["markdown"]
    visible = _visible(markdown)
    assert not any(t.type == "heading_open" for t in MarkdownIt().parse(visible))
    assert "Teach humor via On Jokes" in visible
    assert "Walk through the book chapter by chapter" in visible
    assert "▸ Next: read the whole book first." in visible
    assert "Owns:" not in visible
    assert "~/Library/books" not in visible
    assert "<details>\n<summary>Details</summary>" in markdown
    assert BRIEF in markdown  # all facts, quotes, paths and constraints remain available
    assert f"*{ORIGIN}*" in markdown
    # Presentation must not replace or truncate the original session input.
    event = admit.call_args.args[1]
    assert event.text == f"{ORIGIN}\n\n{BRIEF}"
    assert event.message_id == "77"
    assert event.source.thread_id == "456"
    assert event.internal and not event.allow_gateway_control


@pytest.mark.asyncio
async def test_short_plain_brief_remains_readable_without_rich_support(monkeypatch):
    adapter = _adapter(False)
    admit = AsyncMock()
    monkeypatch.setattr("gateway.wake.admit_internal_event", admit)
    dest = SessionSource(platform=Platform.TELEGRAM, chat_id="123", thread_id="456")
    brief = "/title is not a command here: read the plan"

    await _deliver_brief(adapter, dest, brief, "Split from the main chat")

    adapter._bot.do_api_request.assert_not_called()
    legacy = adapter._bot.send_message.call_args.kwargs["text"]
    assert "read the plan" in legacy
    assert "Split from the main chat" in legacy
    assert admit.call_args.args[1].text == f"Split from the main chat\n\n{brief}"


@pytest.mark.asyncio
async def test_long_unstructured_brief_has_bounded_preview_and_lossless_details(monkeypatch):
    adapter = _adapter()
    admit = AsyncMock()
    monkeypatch.setattr("gateway.wake.admit_internal_event", admit)
    dest = SessionSource(platform=Platform.TELEGRAM, chat_id="123", thread_id="456")
    brief = "**Objective:** " + "Practice with examples " * 35 + "\n\nKeep this final constraint."

    await _deliver_brief(adapter, dest, brief)

    markdown = adapter._bot.do_api_request.call_args.kwargs["api_kwargs"]["rich_message"]["markdown"]
    assert len(_visible(markdown)) < 400
    assert brief in markdown
    assert admit.call_args.args[1].text == brief


@pytest.mark.parametrize("brief", [
    "# Short title\n\nPractice *one* idea.\n\n**Next action:** read chapter 1. Then drill it.",
    "Short title\nPractice *one* idea.\n---\nNext: read chapter 1. Then drill it.",
])
def test_heading_styles_and_formatted_action_labels(brief):
    from gateway.telegram_topic_sessions import _opening_brief_display
    from plugins.platforms.telegram.rich_delivery import rich_message_payload

    display = _opening_brief_display(brief, "Fallback title", ORIGIN)
    markdown = rich_message_payload(display)["markdown"]
    visible = _visible(markdown)
    assert visible.startswith("**Short title**")
    assert "Practice one idea." in visible
    assert "▸ Next: read chapter 1." in visible
    assert "Then drill it." not in visible
    assert brief in markdown
    assert not any(t.type == "heading_open" for t in MarkdownIt().parse(visible))


def test_nested_disclosures_cannot_expose_secondary_details():
    import html
    from gateway.telegram_topic_sessions import _opening_brief_display

    brief = "Practice the book.\n\n<details><summary>Paths</summary>\n`~/book.epub`\n</details>\nFinal constraint."
    display = _opening_brief_display(brief, "Book study", "")
    assert display.count("<details>") == display.count("</details>") == 1
    details = display.split("<summary>Details</summary>\n\n", 1)[1].removesuffix("\n\n</details>")
    assert html.unescape(details) == brief
    assert "▸ Next:" not in display  # Never invent work missing from the brief.
