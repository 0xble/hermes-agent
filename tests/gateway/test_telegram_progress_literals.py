"""Tool-progress fragments stay literal on Telegram's rich and MarkdownV2 wire payloads.

A progress line interpolates a tool name and argument preview into Markdown. Without a literal
boundary, ``mcp__paper__get_guide`` rendered as ``mcp**paper**get_guide``, ``README.md`` became a
link, ``**/*.md`` lost its asterisks and ``\\s+`` lost its backslash.
"""

import queue
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.display import ToolPreview
from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.run_turn_runner import TurnRunner
from gateway.stream_events import ToolCallChunk
from plugins.platforms.telegram.adapter import TelegramAdapter

REGEX = r"^\s+(foo|bar)\s*$"
# (tool, preview) pairs from the reported screenshot, plus a preview with embedded backticks.
SCREENSHOT_CALLS = [
    ("mcp__paper__get_guide", "references/index.md"),
    ("mcp__mobbin__search_sections", "Ease website sections for therapists,"),
    ("read_file", "README.md"),
    ("search_files", "**/*.md"),
    ("search_files", REGEX),
    ("vision_analyze", "why does `render()` emit ``x``?"),
]
LITERALS = [
    "references/index.md", "README.md", "**/*.md", REGEX, "Ease website sections for therapists,",
    "why does `render()` emit ``x``?",
]


def _telegram(mode="always"):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={"rich_messages": mode}))
    bot = MagicMock()
    bot.do_api_request = AsyncMock(return_value=SimpleNamespace(message_id=77))
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=78))
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=78))
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    adapter._telegram_chat_outbound_slot_secs = 0.0  # pacing is not under test here
    return adapter


def _runner(adapter, mode="all"):
    ctx = SimpleNamespace(source=None, progress_mode=mode, last_was_terminal_block=[False], progress_queue=queue.Queue())
    return TurnRunner(SimpleNamespace(_delivery_adapter_for=lambda _source: adapter), ctx), ctx


def _lines(adapter, mode="all"):
    runner, ctx = _runner(adapter, mode)
    out = []
    for tool, preview in SCREENSHOT_CALLS:
        line = runner._progress_build_message(tool, preview, {})
        out.append(ctx.progress_queue.get_nowait() if line is None else line)
    return out


def _code_spans(markdown):
    """Contents of the CommonMark inline code spans in ``markdown`` (one padding space stripped)."""
    spans, pos = [], 0
    while (m := re.compile(r"`+").search(markdown, pos)):
        close = re.compile(rf"(?<!`){m.group(0)}(?!`)").search(markdown, m.end())
        if close is None:
            pos = m.end()
            continue
        body = markdown[m.end():close.start()]
        if body.startswith(" ") and body.endswith(" ") and body.strip():
            body = body[1:-1]
        spans.append(body)
        pos = close.end()
    return spans


def _mdv2_code_spans(text):
    """Decoded contents of MarkdownV2 single-backtick code entities (inside, only \\ and ` are escaped)."""
    spans = re.findall(r"(?<!\\)`((?:[^`\\]|\\[\s\S])+)`", text)
    return [re.sub(r"\\([\\`])", r"\1", span) for span in spans]


def test_progress_builder_uses_friendly_mcp_label_and_literal_preview():
    line = _runner(_telegram())[0]._progress_build_message(
        "mcp__mobbin__search_sections", "Ease website sections for therapists,", {})

    assert line == '🔌 `Mobbin · search sections`: "`Ease website sections for therapists,`"'


@pytest.mark.parametrize("mode", ["all", "verbose"])
def test_every_progress_fragment_is_a_code_span_in_the_rich_payload(mode):
    adapter = _telegram()
    markdown = adapter._rich_message_payload("\n".join(_lines(adapter, mode)))["markdown"]
    spans = _code_spans(markdown)

    for literal in LITERALS:
        assert literal in spans, (literal, markdown)
    assert "Paper · get guide" in spans
    assert "mcp__paper__get_guide" not in markdown


def test_every_progress_fragment_is_a_code_entity_in_markdownv2():
    adapter = _telegram()
    spans = _mdv2_code_spans(adapter.format_message("\n".join(_lines(adapter))))

    for literal in LITERALS:
        assert literal in spans, (literal, spans)
    assert "Paper · get guide" in spans


def test_verbose_args_dump_is_literal():
    adapter = _telegram()
    runner, ctx = _runner(adapter, "verbose")
    runner._progress_build_message("mcp__paper__get_guide", "x", {"path": "**/__init__.md", "re": REGEX})
    line = ctx.progress_queue.get_nowait()

    spans = _mdv2_code_spans(adapter.format_message(line))
    assert '{"path": "**/__init__.md", "re": "^\\\\s+(foo|bar)\\\\s*$"}' in spans
    assert "['path', 're']" in spans


@pytest.mark.asyncio
async def test_progress_send_and_edit_reach_telegram_literally_on_both_parse_paths():
    """The progress lane's own calls: a send with progress metadata, then finalize=True edits
    (Telegram sets REQUIRES_EDIT_FINALIZE). ``always`` takes the rich API; ``never`` MarkdownV2."""
    for mode in ("always", "never"):
        adapter = _telegram(mode)
        runner, ctx = _runner(adapter)
        ctx.source = SimpleNamespace(chat_id="12345")
        ctx._progress_reply_to, ctx._progress_metadata, ctx.progress_grouping = None, None, "edit"
        ctx._cleanup_progress = False
        st = runner._progress_edit_state(adapter)
        lines = _lines(adapter)
        await runner._send_progress_text(st, lines[0])
        await runner._edit_progress_message(st, "78", "\n".join(lines))

        if mode == "always":
            calls = adapter._bot.do_api_request.await_args_list
            assert [c.args[0] for c in calls] == ["sendRichMessage", "editMessageText"]
            spans = _code_spans(calls[-1].kwargs["api_kwargs"]["rich_message"]["markdown"])
        else:
            adapter._bot.do_api_request.assert_not_called()
            assert adapter._bot.send_message.await_args.kwargs["parse_mode"] is not None
            spans = _mdv2_code_spans(adapter._bot.edit_message_text.await_args.kwargs["text"])
        for literal in LITERALS:
            assert literal in spans, (mode, literal)


@pytest.mark.parametrize("text, expected", [
    ("README.md", "`README.md`"),
    ("a `b` c", "``a `b` c``"),
    ("`edge`", "`` `edge` ``"),
    ("x ``y`` z", "```x ``y`` z```"),
    (" padded ", "`  padded  `"),
    ("two\nlines", "`two lines`"),
])
def test_telegram_literal_uses_a_longer_backtick_run_than_its_content(text, expected):
    assert _telegram().format_progress_literal(text) == expected


def test_markdownv2_keeps_single_backtick_code_and_real_fences_unchanged():
    adapter = _telegram()
    assert adapter.format_message("run `a_b` now") == "run `a_b` now"
    assert adapter.format_message(adapter.format_progress_literal("a `b` c")) == "`a \\`b\\` c`"
    fenced = "```\nx = ``y``\n```"
    assert adapter.format_message(fenced) == "```\nx = \\`\\`y\\`\\`\n```"


def test_compact_url_preview_stays_plain_for_auto_linking():
    adapter = _telegram()
    url = "https://example.com/docs/page"
    assert adapter.format_tool_preview(ToolPreview(url)) == url
    assert adapter.format_tool_preview(ToolPreview("example.com/docs/pa...", truncated=True, url=url)) == (
        "example.com/docs/pa...")
    assert adapter.format_tool_preview(ToolPreview("README.md")) == "`README.md`"


def test_stream_event_formatter_shares_the_literal_path():
    adapter = _telegram()
    line = adapter.format_tool_event(
        ToolCallChunk("mcp__paper__get_guide", preview="references/index.md", args={"path": "references/index.md"}),
        mode="verbose", preview_max_len=0)

    assert line.startswith("🔌 `Paper · get guide`(")
    assert '`{"path": "references/index.md"}`' in line


def test_base_hook_is_identity_and_base_preview_is_unchanged():
    concrete = type("Concrete", (BasePlatformAdapter,), {})
    concrete.__abstractmethods__ = frozenset()
    adapter = concrete.__new__(concrete)
    assert adapter.format_progress_literal("**/*.md") == "**/*.md"
    assert adapter.format_tool_preview(ToolPreview("**/*.md")) == "**/*.md"
