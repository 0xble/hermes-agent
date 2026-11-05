"""Actual Telegram wire boundaries for the inline/plain copy contract."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.copy_blocks import copy_free_text_for
from gateway.platforms.base import utf16_len
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.fixture
def adapter(monkeypatch):
    import plugins.platforms.telegram.adapter as tg

    obj = TelegramAdapter(PlatformConfig(enabled=True, token="test"))
    obj._bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=2)),
                               edit_message_text=AsyncMock(), send_message_draft=AsyncMock(return_value=True))
    obj._send_flood_cooldown_remaining = lambda _chat: None
    obj._chat_outbound_slot_remaining = lambda _chat: 0
    obj._hold_chat_outbound_slot = lambda _chat: None
    obj._chat_budget = lambda: SimpleNamespace(note_interim=lambda _chat: None)
    obj._thread_kwargs_for_draft = lambda *_a: {}
    obj._thread_kwargs_for_send = lambda *_a, **_k: {}
    obj._retrigger_typing = AsyncMock()
    monkeypatch.setattr(tg, "skip_progress_edit", lambda *_a: False)
    monkeypatch.setattr(tg, "call_with_flood_guard", lambda _adapter, _chat, call: call())
    return obj


def _reply(adapter):
    return copy_free_text_for(adapter, "Choose:\n[[copy]]\n*literal* `code`\n[[/copy]]\nDone.")


@pytest.mark.asyncio
@pytest.mark.parametrize("finalize", [False, True])
async def test_interim_and_final_edit_wire_quotes_are_literal(adapter, finalize):
    result = await adapter._edit_message_locked("123", "1", _reply(adapter), finalize=finalize)
    assert result.success
    wire = adapter._bot.edit_message_text.call_args.kwargs
    assert wire["parse_mode"] == "MarkdownV2"
    assert wire["text"] == "Choose:\n> \\*literal\\* \\`code\\`\nDone\\."


@pytest.mark.asyncio
@pytest.mark.parametrize("rich_drafts", [False, True])
@pytest.mark.parametrize("parse_fails", [False, True])
async def test_draft_wire_quotes_override_raw_rich_preview_and_fallback_safely(adapter, rich_drafts, parse_fails):
    from telegram.error import BadRequest

    adapter._rich_messages_enabled = True
    adapter._rich_message_mode = "always"
    adapter._rich_drafts_enabled = rich_drafts
    if parse_fails:
        adapter._bot.send_message_draft.side_effect = [BadRequest("can't parse entities"), True]
    result = await adapter.send_draft("123", 1, _reply(adapter))
    assert result.success
    wires = [call.kwargs for call in adapter._bot.send_message_draft.call_args_list]
    assert wires[0]["parse_mode"] == "MarkdownV2"
    assert wires[0]["text"] == "Choose:\n> \\*literal\\* \\`code\\`\nDone\\."
    if parse_fails:
        assert "parse_mode" not in wires[1]
        assert wires[1]["text"] == "Choose:\n*literal* `code`\nDone."
    assert all("\x00" not in wire["text"] for wire in wires)


@pytest.mark.asyncio
@pytest.mark.parametrize("finalize", [False, True])
async def test_overflow_first_edit_and_continuations_never_expose_display_markers(adapter, finalize):
    raw = "[[copy]]\n" + "\n".join(["*literal* `code`"] * 600) + "\n[[/copy]]"
    result = await adapter._edit_overflow_split("123", "1", copy_free_text_for(adapter, raw), finalize=finalize)
    assert result.success
    wires = [adapter._bot.edit_message_text.call_args.kwargs]
    wires += [call.kwargs for call in adapter._bot.send_message.call_args_list]
    assert len(wires) > 1
    for wire in wires:
        assert wire["parse_mode"] == "MarkdownV2"
        assert "\x00" not in wire["text"] and utf16_len(wire["text"]) <= 4096
        assert wire["text"].startswith("**> \\*literal\\* \\`code\\`")
        assert "||" in wire["text"]


@pytest.mark.asyncio
async def test_stream_fallback_send_keeps_copy_quote_at_wire_boundary(adapter):
    async def send(chat_id, content, reply_to=None, metadata=None):
        return await adapter._send_text_locked(chat_id, content, reply_to, metadata)

    async def chunks(chat_id, payloads, _delivered, _reply, _metadata, _errors):
        for payload in payloads:
            await adapter._send_chunk_markdown_or_plain(payload, {"chat_id": chat_id})
        return SimpleNamespace(success=True, message_id="2")

    adapter.send = send
    adapter._send_chunks = chunks
    adapter.delete_message = AsyncMock(return_value=True)
    consumer = GatewayStreamConsumer(adapter, "123", StreamConsumerConfig(cursor=""))
    reply = "Choose:\n[[copy]]\n*literal* `code`\n[[/copy]]\nDone."
    await consumer._send_fallback_final(copy_free_text_for(adapter, reply))
    wire = adapter._bot.send_message.call_args.kwargs
    assert wire["parse_mode"] == "MarkdownV2"
    assert wire["text"] == "Choose:\n> \\*literal\\* \\`code\\`\nDone\\."


@pytest.mark.asyncio
async def test_replay_inline_row_and_separate_copy_row_do_not_duplicate(adapter, monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    import gateway.delivery_ledger as dl

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._obligation_adapter = AsyncMock(return_value=adapter)
    runner._redelivery_restart_note_event = AsyncMock(return_value=None)
    runner._arm_flood_timers_for_waiting_rows = AsyncMock()
    monkeypatch.setattr(dl, "mark_delivered", lambda *_a: None)
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="2"))
    inline = _reply(adapter)
    rows = [{"obligation_id": oid, "platform": "telegram", "chat_id": "123", "thread_id": None,
             "content": content, "attempts": 1, "session_key": "s"}
            for oid, content in [("main", inline), ("copy", "[[copy]]\n*literal* `code`\n[[/copy]]")]]
    assert await runner._redeliver_claimed_obligations(rows) == 2
    sent = [call.kwargs for call in adapter.send.call_args_list]
    assert [call["content"] for call in sent] == [inline, "*literal* `code`"]
    assert sent[1]["metadata"] == {"copy_block": True, "plain": True}
    assert adapter.format_message(sent[0]["content"]) == "Choose:\n> \\*literal\\* \\`code\\`\nDone\\."


@pytest.mark.asyncio
async def test_send_parse_fallback_keeps_literal_quote_body_and_plain_copy_unchanged(adapter):
    from telegram.error import BadRequest

    adapter._bot.send_message.side_effect = [BadRequest("can't parse entities"), SimpleNamespace(message_id=2)]
    await adapter._send_chunk_markdown_or_plain(adapter.format_message(_reply(adapter)), {"chat_id": "123"})
    wires = [call.kwargs for call in adapter._bot.send_message.call_args_list]
    assert wires[1]["parse_mode"] is None
    assert wires[1]["text"] == "Choose:\n> *literal* `code`\nDone."
    adapter._bot.send_message.reset_mock(side_effect=True)
    await adapter._send_chunk_markdown_or_plain("*literal* `code`", {"chat_id": "123"}, plain=True)
    assert adapter._bot.send_message.call_args.kwargs["text"] == "*literal* `code`"
    assert adapter._bot.send_message.call_args.kwargs["parse_mode"] is None


@pytest.mark.asyncio
async def test_interim_edit_parse_fallback_never_exposes_display_markers(adapter):
    from telegram.error import BadRequest

    adapter._bot.edit_message_text.side_effect = [BadRequest("can't parse entities"), None]
    assert (await adapter._edit_message_locked("123", "1", _reply(adapter), finalize=False)).success
    wire = adapter._bot.edit_message_text.call_args.kwargs
    assert "parse_mode" not in wire
    assert wire["text"] == "Choose:\n*literal* `code`\nDone."


@pytest.mark.asyncio
async def test_long_rich_draft_keeps_quote_and_encoded_length_bound(adapter):
    adapter._rich_messages_enabled = True
    adapter._rich_message_mode = "always"
    adapter._rich_drafts_enabled = False
    # Raw source fits, MarkdownV2 doesn't: the preview still needs an encoded-length preflight.
    reply = copy_free_text_for(adapter, "[[copy]]\n" + "*" * 3000 + "\n[[/copy]]")
    assert (await adapter.send_draft("123", 1, reply)).success
    wire = adapter._bot.send_message_draft.call_args.kwargs
    assert wire["parse_mode"] == "MarkdownV2" and wire["text"].startswith("> \\*")
    assert "\x00" not in wire["text"] and utf16_len(wire["text"]) <= 4096


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["send", "draft", "edit"])
@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("line_end", ["\n", "\r", "\r\n"])
async def test_many_short_copy_lines_fit_encoded_wire_on_send_and_stream(adapter, operation, internal, line_end):
    raw = "[[copy]]\n" + line_end.join(["*"] * 1000) + "\n[[/copy]]"
    content = copy_free_text_for(adapter, raw) if internal else raw
    if operation == "send":
        async def chunks(chat_id, payloads, *_args):
            for payload in payloads:
                await adapter._send_chunk_markdown_or_plain(payload, {"chat_id": chat_id})
            return SimpleNamespace(success=True, message_id="2")

        adapter._send_chunks = chunks
        result = await adapter._send_text_locked("123", content, None, None)
        wires = [call.kwargs for call in adapter._bot.send_message.call_args_list]
        assert len(wires) > 1
    elif operation == "draft":
        result = await adapter.send_draft("123", 1, content)
        wires = [adapter._bot.send_message_draft.call_args.kwargs]
    else:
        result = await adapter._edit_message_locked("123", "1", content, finalize=False)
        wires = [adapter._bot.edit_message_text.call_args.kwargs]
    assert result.success
    for wire in wires:
        assert wire["parse_mode"] == "MarkdownV2"
        assert wire["text"].startswith("**> \\*") and "||" in wire["text"]
        assert "\x00" not in wire["text"] and utf16_len(wire["text"]) <= 4096
