"""Telegram keeps copy context inline without changing the separate paste payload."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.copy_blocks import extract_copy_blocks
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("lines", [1, 6, 7])
async def test_copy_quotes_match_stream_and_final_with_literal_bodies(lines):
    body = "\n".join(["*literal* `x` [link](url) > || \\"] * lines)
    reply = f"First:\n[[copy]]\n{body}\n[[/copy]]\nSecond:\n[[copy]]\n```\nMEDIA:/literal.png\n```\n[[/copy]]\nDone."
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = "telegram"
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="m"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="m"))
    event = SimpleNamespace(source=SimpleNamespace(chat_id="c", platform="telegram"))
    extracted = await adapter._extract_response_content(reply, event, "", is_ephemeral_response=True)
    assert extracted.copy_blocks == [body, "```\nMEDIA:/literal.png\n```"]
    assert not (extracted.images or extracted.media_files or extracted.local_files)
    formatted = adapter.format_message(extracted.text_content)
    quote = "\n".join([r"> \*literal\* \`x\` \[link\]\(url\) \> \|\| \\"] * lines)
    if lines > 6:
        quote = "**" + quote + "||"
    expected = "First:\n" + quote + "\nSecond:\n> \\`\\`\\`\n> MEDIA:/literal\\.png\n> \\`\\`\\`\nDone\\."
    assert formatted == expected
    assert not adapter._rich_eligible(extracted.text_content)

    consumer = GatewayStreamConsumer(adapter, "c", config=StreamConsumerConfig(cursor=""))
    # Both preview send and edit-in-place final must retain the quote, including split markers.
    for char in reply:
        consumer._append_accumulated(char)
    consumer._flush_copy_filter()
    await consumer._send_or_edit(consumer._accumulated)
    consumer._adopt_final_text(reply)
    await consumer._send_or_edit(consumer._accumulated, finalize=True)
    for call in (adapter.send.call_args, adapter.edit_message.call_args):
        assert adapter.format_message(call.kwargs["content"]) == expected
    assert consumer.delivered_final_matches(reply) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["telegram", "discord"])
async def test_literal_copy_directives_and_separate_payloads_survive(platform):
    from tests.gateway.test_copy_blocks import _FakeAdapter
    from gateway.streaming_tts_consumer import StreamingTTSConsumer

    adapter = object.__new__(_FakeAdapter)
    adapter.platform = platform
    body = "  exact\r\nMEDIA:/literal.png\r\n![literal](https://example.com/a.png)  "
    reply = "Before\n[[copy]]\n" + body + "\n[[/copy]]\nAfter"
    event = SimpleNamespace(source=SimpleNamespace(chat_id="c", platform=platform))
    extracted = await adapter._extract_response_content(reply, event, "", is_ephemeral_response=True)
    assert not (extracted.images or extracted.media_files or extracted.local_files)
    if platform == "telegram":
        assert extracted.copy_blocks == [body]
        sent = []

        async def send_final_ledgered(_event, _session, content, metadata, **kwargs):
            sent.append((content, metadata))
            return SimpleNamespace(success=True), adapter

        adapter.send_final_ledgered = send_final_ledgered
        await adapter._send_copy_blocks(event, "s", extracted.copy_blocks, {}, lambda _: None)
        assert sent[0][0].encode() == body.encode()
        assert sent[0][1]["copy_block"] and sent[0][1]["plain"]
    else:
        assert extracted.copy_blocks == []
        assert extracted.text_content == "Before\n" + body + "\nAfter"
    consumer = object.__new__(StreamingTTSConsumer)
    consumer._aborted = consumer._finished = False
    consumer._streamer = object()
    spoken = []
    consumer._chunker = SimpleNamespace(feed=lambda t: [t], flush=list)
    consumer._enqueue_clauses = lambda clauses, *_a, **_k: spoken.extend(clauses)
    for char in reply:
        consumer.on_delta(char)
    assert "".join(spoken) == "Before\nAfter"
    assert extract_copy_blocks(reply)[1] == [body]


def test_copy_quote_routing_escaping_and_replay_do_not_promote_inline_copies():
    from gateway.copy_blocks import copy_free_text_for, split_copy_blocks_for
    from plugins.platforms.telegram.adapter import _normalize_dollar_entities, _strip_mdv2

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = "telegram"
    adapter._rich_message_mode = "always"
    adapter._rich_drafts_enabled = True
    raw = "Cost &#36;5\n[[copy]]\n*literal* &#36;5 \u2014 $$math$$\n[[/copy]]\nEnd"
    inline, separate = split_copy_blocks_for(adapter, raw)
    assert separate == ["*literal* &#36;5 — $$math$$"]
    assert split_copy_blocks_for(adapter, inline) == (inline, [])
    assert split_copy_blocks_for(adapter, inline, inline_quotes=False) == (inline, [])
    assert split_copy_blocks_for(adapter, "[[copy]]\nexact\n[[/copy]]", inline_quotes=False) == ("", ["exact"])
    assert copy_free_text_for(adapter, inline) == inline
    from gateway.copy_blocks import strip_copy_blocks
    assert strip_copy_blocks(inline) == "Cost &#36;5\nEnd"
    nested = copy_free_text_for(adapter, "[[copy]]\n```\n[[copy]]\nliteral\n```\n[[/copy]]")
    assert split_copy_blocks_for(adapter, nested) == (nested, [])
    assert split_copy_blocks_for(adapter, nested, inline_quotes=False) == (nested, [])
    normalized = _normalize_dollar_entities(inline)
    assert "Cost $5" in normalized and "*literal* &#36;5" in normalized
    assert "*literal* &#36;5" in _strip_mdv2(inline)
    for text in [raw, inline]:
        assert "\x00" not in adapter.format_message(text)


def test_two_short_quotes_and_one_expandable_quote_stay_in_place():
    adapter = object.__new__(TelegramAdapter)
    reply = ("Step 1:\n[[copy]]\nalpha *literal*\n[[/copy]]\n"
             "Step 2:\n[[copy]]\nbeta_code\n[[/copy]]\nStep 3:\n[[copy]]\n"
             + "\n".join(f"Line {n}" for n in range(1, 8)) + "\n[[/copy]]\nDone.")
    expected = ("Step 1:\n> alpha \\*literal\\*\nStep 2:\n> beta\\_code\n"
                "Step 3:\n**> Line 1\n> Line 2\n> Line 3\n> Line 4\n> Line 5\n> Line 6\n> Line 7||\nDone\\.")
    assert adapter.format_message(reply) == expected


@pytest.mark.asyncio
async def test_interrupted_stream_does_not_send_copy_body_a_second_time():
    from gateway.run_turn import GatewayTurnMixin

    runner = object.__new__(GatewayTurnMixin)
    adapter = SimpleNamespace(platform="telegram", send=AsyncMock(),
                              _streaming_tts_turn_completed=lambda *_a: False)
    runner._delivery_adapter_for = lambda _source: adapter
    runner._should_send_voice_reply = lambda *_a, **_k: False
    runner._deliver_media_from_response = AsyncMock(return_value=False)
    event = SimpleNamespace(source=SimpleNamespace(chat_id="c", platform="telegram"), text="")
    await runner._hmwa_deliver_turn_response(
        event, event.source, None, "s", None, {"already_sent": True, "interrupted": True},
        [], "[[copy]]\npartial", None, False)
    adapter.send.assert_not_called()


@pytest.mark.parametrize("body", ["*" * 9000, "\n".join(["```", "*code*", "```"] * 150)], ids=["huge-line", "fenced-lines"])
def test_oversized_copy_quotes_balance_and_escape_each_api_chunk(body):
    from gateway.copy_blocks import copy_free_text_for
    from gateway.platforms.base import utf16_len
    from plugins.platforms.telegram.copy_quotes import truncate_copy_source

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = "telegram"
    for reply in ["[[copy]]\n" + body + "\n[[/copy]]",
                  copy_free_text_for(adapter, "[[copy]]\n" + body + "\n[[/copy]]")]:
        chunks = truncate_copy_source(reply, 4096, utf16_len, 3, close_chunks=True)
        assert len(chunks) > 1
        for chunk in chunks:
            wire = adapter.format_message(chunk)
            assert utf16_len(wire) <= 4096
            assert "\x00" not in wire
            assert wire.startswith(("> ", "**> "))
            if wire.startswith("**>"):
                assert "||\n" in wire
            assert "```" not in wire


@pytest.mark.asyncio
async def test_telegram_final_send_uses_balanced_copy_chunks_at_api_boundary():
    from gateway.platforms.base import utf16_len

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = "telegram"
    adapter._text_send_refusal = lambda: None
    adapter._send_flood_cooldown_remaining = lambda _chat: None
    adapter._chat_outbound_slot_remaining = lambda _chat: 0
    adapter._hold_chat_outbound_slot = lambda _chat: None
    adapter._telegram_error_types = lambda: ()
    adapter._retrigger_typing = AsyncMock()
    observed = []

    async def capture(_chat, chunks, _delivered, *_args):
        observed.extend(chunks)
        return SimpleNamespace(success=True)

    adapter._send_chunks = capture
    reply = "[[copy]]\n" + "\n".join(["*literal* `code`"] * 600) + "\n[[/copy]]"
    result = await adapter._send_text_locked("c", reply, None, None)
    assert result.success and len(observed) > 1
    for wire in observed:
        assert utf16_len(wire) <= 4096
        assert wire.startswith("**> ") and "||\n" in wire
        assert "\x00" not in wire and "[[copy]]" not in wire
