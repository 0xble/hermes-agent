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


def _adapter(mode: bool | str | None = "always"):
    extra = {} if mode is None else {"rich_messages": mode}
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra=extra))
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
    assert "<details" not in legacy and "<summary" not in legacy
    assert legacy.count("read the plan") == 1


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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,suffix,max_chars,refusal", [
    pytest.param(None, "", None, "", id="default"),
    pytest.param(False, "", None, "", id="disabled"),
    pytest.param("always", "\n\n中文说明", None, "", id="cjk"),
    pytest.param("always", "\n\n$$x^2$$", None, "", id="math"),
    pytest.param("always", "", 200, "", id="oversize"),
    pytest.param("always", "", None, "capability", id="capability"),
    pytest.param("always", "", None, "bad_request", id="bad_request"),
])
async def test_brief_fallback_sends_original_once_without_rich_markup(monkeypatch, mode, suffix, max_chars, refusal):
    from telegram.error import BadRequest

    adapter = _adapter(mode)
    brief = BRIEF + "\n\nNext: run x_y. Keep `C:\\work\\my_file` and $5." + suffix
    if max_chars:
        # Lower the existing limit, not the eligibility decision or send path.
        adapter.RICH_MESSAGE_MAX_CHARS = max_chars
    if refusal:
        adapter._bot.do_api_request.side_effect = {
            "capability": NotImplementedError("no such method"),
            "bad_request": BadRequest("rich_message has too many blocks"),
        }[refusal]
    admit = AsyncMock()
    monkeypatch.setattr("gateway.wake.admit_internal_event", admit)
    dest = SessionSource(platform=Platform.TELEGRAM, chat_id="123", thread_id="456", profile="study")

    await _deliver_brief(adapter, dest, brief, ORIGIN, title="Fix my_file")

    calls = adapter._bot.send_message.call_args_list
    assert len(calls) == 1
    text = calls[0].kwargs["text"]
    assert text == adapter.format_message(f"{ORIGIN}\n\n{brief}")
    assert "<details" not in text and "<summary" not in text
    assert text.count("Walk through the book") == 1
    assert r"x\_y" in text and r"x\\\_y" not in text
    assert calls[0].kwargs["message_thread_id"] == 456
    assert adapter._bot.do_api_request.call_count == int(bool(refusal))
    event = admit.call_args.args[1]
    assert event.text == f"{ORIGIN}\n\n{brief}"
    assert event.message_id == event.source.message_id == "77"
    assert event.source.thread_id == "456" and event.source.profile == "study"
    assert event.internal and not event.allow_gateway_control


@pytest.mark.asyncio
async def test_wrapped_prose_uses_topic_name_and_rich_timeout_does_not_resend(monkeypatch):
    from telegram.error import TimedOut

    adapter = _adapter()
    brief = "Please read the plan at\n~/x and then do it."
    admit = AsyncMock()
    monkeypatch.setattr("gateway.wake.admit_internal_event", admit)
    dest = SessionSource(platform=Platform.TELEGRAM, chat_id="123", thread_id="456")

    await _deliver_brief(adapter, dest, brief, title="Read my_file")

    markdown = adapter._bot.do_api_request.call_args.kwargs["api_kwargs"]["rich_message"]["markdown"]
    visible = _visible(markdown)
    assert visible.startswith(r"**Read my\_file**")
    assert "Please read the plan at ~/x and then do it." in visible
    assert brief in markdown
    assert admit.call_args.args[1].text == brief
    adapter._bot.send_message.assert_not_called()

    admit.reset_mock()
    adapter._bot.do_api_request.side_effect = TimedOut("Timed out")
    with pytest.raises(RuntimeError, match="Timed out"):
        await _deliver_brief(adapter, dest, brief, title="Read my_file")
    # An ambiguous rich timeout may have delivered. Never duplicate it with legacy.
    adapter._bot.send_message.assert_not_called()
    admit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("title_line", [
    "Mac Studio Crash", "**Mac Studio Crash**", "# Mac Studio Crash",
])
@pytest.mark.parametrize("objective", ["", "Investigate the panic report."])
@pytest.mark.parametrize("separator", ["\n", "\n\n"])
async def test_title_paragraph_is_not_repeated_as_objective(monkeypatch, title_line, objective, separator):
    adapter = _adapter()
    admit = AsyncMock()
    monkeypatch.setattr("gateway.wake.admit_internal_event", admit)
    dest = SessionSource(platform=Platform.TELEGRAM, chat_id="123", thread_id="456")
    brief = separator.join(filter(None, [
        title_line, objective,
        "Next: read the panic report and the existing evidence note, then test the hypotheses below.",
        "Keep the original constraints.",
    ]))

    await _deliver_brief(adapter, dest, brief, ORIGIN, title="Mac Studio Crash")

    markdown = adapter._bot.do_api_request.call_args.kwargs["api_kwargs"]["rich_message"]["markdown"]
    visible = _visible(markdown)
    # Rich delivery uses NBSP spacer rows, not separate Markdown paragraphs.
    visible_text = " ".join("".join(
        child.content if child.type == "text" else " "
        for token in MarkdownIt().parse(visible) if token.type == "inline"
        for child in token.children
        if child.type in {"text", "softbreak", "hardbreak"}
    ).split())
    assert visible.startswith("**Mac Studio Crash**")
    assert visible_text == " ".join([
        "Mac Studio Crash", *([objective] if objective else []),
        "▸ Next: read the panic report and the existing evidence note, then test the hypotheses below.",
    ])
    assert brief in markdown
    assert "<details>\n<summary>Details</summary>" in markdown
    assert "<details open" not in markdown
    assert admit.call_args.args[1].text == f"{ORIGIN}\n\n{brief}"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [
    "Next action:\nread the panic report and the evidence note, then test the hypotheses.",
    "**Next action:**\nread the panic report and the evidence note, then test the hypotheses.",
    "Next action:  \nread the panic report and the evidence note, then test the hypotheses.",
    "Next: read the panic report and the evidence note,\nthen test the hypotheses.",
])
@pytest.mark.parametrize("separator", ["\n", "\n\n"])
async def test_wrapped_next_action_stays_visible_in_full(monkeypatch, action, separator):
    adapter = _adapter()
    admit = AsyncMock()
    monkeypatch.setattr("gateway.wake.admit_internal_event", admit)
    dest = SessionSource(platform=Platform.TELEGRAM, chat_id="123", thread_id="456")
    brief = separator.join([
        "**Mac Studio Crash**", "Investigate the panic report.", action,
        "Keep the original constraints.",
    ])

    await _deliver_brief(adapter, dest, brief, ORIGIN, title="Mac Studio Crash")

    assert adapter._bot is not None
    markdown = adapter._bot.do_api_request.call_args.kwargs["api_kwargs"]["rich_message"]["markdown"]
    visible = " ".join(_visible(markdown).split())
    assert visible == (
        "**Mac Studio Crash** Investigate the panic report. "
        "▸ Next: read the panic report and the evidence note, then test the hypotheses."
    )
    assert brief in markdown
    assert "<details>\n<summary>Details</summary>" in markdown
    assert "<details open" not in markdown
    assert admit.call_args.args[1].text == f"{ORIGIN}\n\n{brief}"
