from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from gateway.copy_blocks import (
    CopyMarkerStreamFilter,
    extract_copy_blocks,
    map_outside_copy_blocks,
    render_copy_blocks_inline,
    strip_copy_blocks,
)
from gateway.platforms.base import BasePlatformAdapter
from gateway.run_notifications import GatewayNotificationsMixin
from gateway.run_turn import GatewayTurnMixin


def test_extract_copy_blocks_preserves_body_and_order() -> None:
    text = "before\n[[copy]]\n*exact*  \nline `x`\nMEDIA:/tmp/literal.txt\n[[/copy]]\nafter\n[[copy]]\ntwo\n[[/copy]]"
    remaining, blocks = extract_copy_blocks(text)
    assert remaining == "before\nafter\n"
    assert blocks == ["*exact*  \nline `x`\nMEDIA:/tmp/literal.txt", "two"]


def test_extract_copy_blocks_fences_outside_are_literal_but_inside_are_body() -> None:
    text = "```\n[[copy]]\nliteral\n[[/copy]]\n```\n[[copy]]\n```\nbody\n```\n[[/copy]]"
    remaining, blocks = extract_copy_blocks(text)
    assert remaining == "```\n[[copy]]\nliteral\n[[/copy]]\n```\n"
    assert blocks == ["```\nbody\n```"]


def test_extract_copy_blocks_malformed_empty_and_stray_close() -> None:
    assert extract_copy_blocks("x\n[[/copy]]\ny")[0] == "x\ny"
    assert extract_copy_blocks("[[copy]]\n  \n[[/copy]]") == ("", [])
    assert extract_copy_blocks("prefix\n[[copy]]\nbody") == ("prefix\n", ["body"])


def test_extract_copy_blocks_empty_and_none_inputs() -> None:
    assert extract_copy_blocks("") == ("", [])
    assert extract_copy_blocks("plain reply") == ("plain reply", [])


def test_extract_copy_blocks_is_idempotent() -> None:
    text = "reply\n[[copy]]\ncode\n[[/copy]]\n"
    first = extract_copy_blocks(text)
    assert extract_copy_blocks(first[0]) == (first[0], [])


def test_extract_copy_blocks_crlf_and_inline_degrade() -> None:
    remaining, blocks = extract_copy_blocks("before\r\n[[copy]]\r\nbody\r\n[[/copy]]")
    assert remaining == "before\r\n"
    assert blocks == ["body"]
    assert render_copy_blocks_inline("before\n[[copy]]\nbody\n[[/copy]]") == "before\nbody\n"


def test_copy_marker_stream_filter_hides_split_markers_and_preserves_fences() -> None:
    stream = CopyMarkerStreamFilter()
    deltas = ["before\n[[co", "py]]\r\nbody\r\n[[/co", "py]]\r\nafter\n```\n[[copy]]\n"]
    rendered = "".join(stream.feed(delta) for delta in deltas) + stream.flush()
    assert rendered == "before\nbody\r\nafter\n```\n[[copy]]\n"


def test_copy_marker_stream_filter_handles_crlf_split_between_deltas() -> None:
    stream = CopyMarkerStreamFilter()
    assert stream.feed("before\r") == ""
    assert stream.feed("\n[[copy]]\r") == "before\r\n"
    assert stream.feed("\nbody\r\n[[/copy]]\r") == "body\r\n"
    assert stream.feed("\nafter") == "after"
    assert stream.flush() == ""


def test_copy_marker_stream_filter_passes_ordinary_partial_lines_immediately() -> None:
    stream = CopyMarkerStreamFilter()
    assert stream.feed("ordinary ") == "ordinary "
    assert stream.feed("text") == "text"
    assert stream.flush() == ""


def test_longer_fence_quotes_shorter_fences_and_markers() -> None:
    text = "````\n[[copy]]\nliteral\n[[/copy]]\n```\n[[copy]]\nbody\n[[/copy]]\n````\n"
    assert extract_copy_blocks(text) == (text, [])
    stream = CopyMarkerStreamFilter()
    assert "".join(stream.feed(ch) for ch in text) + stream.flush() == text


def test_fenced_marker_inside_copy_body_is_body_text() -> None:
    text = "[[copy]]\n```\n[[/copy]]\n```\n[[/copy]]\nafter\n"
    remaining, blocks = extract_copy_blocks(text)
    assert blocks == ["```\n[[/copy]]\n```"]
    assert remaining == "after\n"
    stream = CopyMarkerStreamFilter()
    rendered = "".join(stream.feed(ch) for ch in text) + stream.flush()
    assert rendered == "```\n[[/copy]]\n```\nafter\n"


def test_tilde_and_backtick_fences_do_not_close_each_other() -> None:
    text = "~~~\n```\n[[copy]]\nx\n[[/copy]]\n~~~\n"
    assert extract_copy_blocks(text) == (text, [])


def test_drop_bodies_filter_hides_blocks_across_any_split() -> None:
    text = "intro\n[[copy]]\nSECRET body\n[[/copy]]\nafter [[ text\n```\n[[copy]]\n```\n"
    expected = strip_copy_blocks(text)
    for size in (1, 2, 3, 5, 8, 13, len(text)):
        stream = CopyMarkerStreamFilter(drop_bodies=True)
        out = "".join(stream.feed(text[i:i + size]) for i in range(0, len(text), size))
        out += stream.flush()
        assert out == expected
        assert "SECRET" not in out


def test_drop_bodies_filter_never_emits_partial_markers() -> None:
    stream = CopyMarkerStreamFilter(drop_bodies=True)
    assert stream.feed("before\n[[co") == "before\n"
    assert stream.feed("py]]\npartial bo") == ""
    assert stream.feed("dy\n[[/co") == ""
    assert stream.feed("py]]\naft") == "aft"
    assert stream.flush() == ""


def test_strip_copy_blocks_leaves_marker_free_text_unchanged() -> None:
    text = "ordinary reply\nwith `code` and [[link]] text"
    assert strip_copy_blocks(text) == text
    stream = CopyMarkerStreamFilter(drop_bodies=True)
    assert stream.feed(text) + stream.flush() == text


def test_inline_render_matches_streamed_render_for_any_split() -> None:
    import random
    rng = random.Random(7)
    pieces = ["a\n", "[[copy]]\n", "[[/copy]]\n", "```\n", "~~~~\n", "x [[copy]]\n", "\r\n", "[[co", "b"]
    for _ in range(300):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 14)))
        stream = CopyMarkerStreamFilter()
        i, out = 0, []
        while i < len(text):
            n = rng.randint(1, 5)
            out.append(stream.feed(text[i:i + n]))
            i += n
        assert "".join(out) + stream.flush() == render_copy_blocks_inline(text), repr(text)


def test_map_outside_copy_blocks_never_rewrites_bodies() -> None:
    def resolve(text: str) -> str:
        return text.replace("MEDIA:/a.png", "data:image/png")

    text = "hi MEDIA:/a.png\n[[copy]]\nMEDIA:/a.png\n[[/copy]]\nbye MEDIA:/a.png\n"
    assert map_outside_copy_blocks(text, resolve) == "hi data:image/png\nMEDIA:/a.png\nbye data:image/png\n"
    kept = map_outside_copy_blocks(text, resolve, keep_markers=True)
    stream = CopyMarkerStreamFilter()
    assert stream.feed(kept) + stream.flush() == map_outside_copy_blocks(text, resolve)
    assert map_outside_copy_blocks("plain MEDIA:/a.png", resolve) == "plain data:image/png"


def test_literal_open_marker_inside_body_is_body_on_every_renderer() -> None:
    text = "a\n[[copy]]\nx\n[[copy]]\ny\n[[/copy]]\nb\n"
    assert extract_copy_blocks(text) == ("a\nb\n", ["x\n[[copy]]\ny"])
    assert render_copy_blocks_inline(text) == "a\nx\n[[copy]]\ny\nb\n"
    stream = CopyMarkerStreamFilter(drop_bodies=True)
    assert "".join(stream.feed(ch) for ch in text) + stream.flush() == "a\nb\n"


def test_extracted_response_keeps_legacy_constructor() -> None:
    from gateway.platforms.base import _ExtractedResponse
    extracted = _ExtractedResponse(
        text_content="", images=[], media_files=[], local_files=[],
        force_document_attachments=False, pre_extract="")
    assert extracted.copy_blocks == []


class _FakeAdapter(BasePlatformAdapter):
    @property
    def name(self):
        return "fake"

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SimpleNamespace(success=True)


@pytest.mark.asyncio
async def test_copy_blocks_use_final_ledger_in_source_order_without_sleep(caplog) -> None:
    adapter = object.__new__(_FakeAdapter)
    calls = []
    caplog.set_level(logging.DEBUG, logger="gateway.platforms.base")

    async def send_final_ledgered(event, session_key, content, metadata, **kwargs):
        calls.append((content, metadata.copy(), kwargs))
        return SimpleNamespace(success=True, message_id=None), adapter

    adapter.send_final_ledgered = send_final_ledgered
    results = []
    await adapter._send_copy_blocks(
        SimpleNamespace(source=SimpleNamespace(chat_id="chat")),
        "session",
        ["first *literal*", "second _literal_"],
        {"notify": True},
        results.append,
    )
    assert [call[0] for call in calls] == ["first *literal*", "second _literal_"]
    assert [call[1]["copy_block_index"] for call in calls] == [0, 1]
    assert all(call[1]["copy_block"] and call[1]["plain"] for call in calls)
    assert all(result.success for result in results)
    assert any("inter-message start gap:" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_copy_block_failure_is_recorded_and_does_not_disappear() -> None:
    adapter = object.__new__(_FakeAdapter)
    results = []
    attempts = 0

    async def send_final_ledgered(event, session_key, content, metadata, **kwargs):
        nonlocal attempts
        attempts += 1
        return SimpleNamespace(success=attempts == 1, error=None if attempts == 1 else "blocked"), adapter

    adapter.send_final_ledgered = send_final_ledgered
    await adapter._send_copy_blocks(
        SimpleNamespace(source=SimpleNamespace(chat_id="chat")), "session", ["ok", "failed"], {}, results.append)
    assert [result.success for result in results] == [True, False]
    assert attempts == 2


@pytest.mark.asyncio
async def test_streamed_copy_blocks_use_ledgered_delivery() -> None:
    runner = object.__new__(GatewayTurnMixin)
    sent = []

    async def send_copy_blocks(event, session, blocks, metadata, record, **kwargs):
        sent.append((blocks, metadata, kwargs))
        record(SimpleNamespace(success=True))

    adapter = SimpleNamespace(
        platform="telegram",
        _streaming_tts_turn_completed=lambda *_args, **_kwargs: False,
        _send_copy_blocks=send_copy_blocks,
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._event_thread_metadata = lambda *_args: {"thread_id": "t"}
    async def no_media(*_args):
        return False
    runner._deliver_media_from_response = no_media
    event = SimpleNamespace(source=SimpleNamespace(chat_id="chat"))
    result = {"already_sent": True, "is_ephemeral_response": True, "ephemeral_ttl": 7}
    returned = await runner._hmwa_deliver_turn_response(
        event, event.source, None, "session", None, result, [], "[[copy]]\nbody\n[[/copy]]", None, False,
    )
    assert returned is None
    assert sent == [(["body"], {"thread_id": "t"}, {"is_ephemeral_response": True, "ephemeral_ttl": 7})]

    # The queued lane already sent (or ledgered) this response's blocks: completion must not.
    sent.clear()
    result = {"already_sent": True, "copy_already_delivered": True}
    await runner._hmwa_deliver_turn_response(
        event, event.source, None, "session", None, result, [], "[[copy]]\nbody\n[[/copy]]", None, False,
    )
    assert sent == []


@pytest.mark.asyncio
async def test_interrupted_turn_does_not_send_partial_copy_block() -> None:
    runner = object.__new__(GatewayTurnMixin)
    sent = []

    async def send_copy_blocks(*args, **kwargs):
        sent.append((args, kwargs))

    adapter = SimpleNamespace(
        _streaming_tts_turn_completed=lambda *_args, **_kwargs: False,
        _send_copy_blocks=send_copy_blocks,
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._event_thread_metadata = lambda *_args: {"thread_id": "t"}
    async def no_media(*_args):
        return False
    runner._deliver_media_from_response = no_media
    event = SimpleNamespace(source=SimpleNamespace(chat_id="chat"))
    returned = await runner._hmwa_deliver_turn_response(
        event, event.source, None, "session", None, {"already_sent": True, "interrupted": True}, [],
        "[[copy]]\npartial", None, False,
    )
    assert returned is None
    assert sent == []


@pytest.mark.asyncio
async def test_copy_block_ledger_rows_are_distinct_and_wrapped(monkeypatch) -> None:
    import gateway.delivery_ledger as ledger

    adapter = object.__new__(_FakeAdapter)
    adapter._final_delivery_adapter = lambda source: adapter
    captured = []
    monkeypatch.setattr(ledger, "ledger_enabled", lambda: True)
    monkeypatch.setattr(ledger, "record_obligation", lambda **kwargs: captured.append(kwargs))
    monkeypatch.setattr(ledger, "mark_attempting", lambda _oid: None)

    async def send_with_retry(**_kwargs):
        return SimpleNamespace(success=True, message_id=None, pre_send=False)

    adapter._send_with_retry = send_with_retry
    event = SimpleNamespace(
        source=SimpleNamespace(chat_id="chat", platform="telegram", thread_id=None), message_id="m1",
        text="request", ledger_message_id=None,
    )
    await adapter.send_final_ledgered(
        event, "session", "same", {"copy_block": True, "copy_block_index": 0}, reply_to=None)
    await adapter.send_final_ledgered(
        event, "session", "same", {"copy_block": True, "copy_block_index": 1}, reply_to=None)
    assert len({row["obligation_id"] for row in captured}) == 2
    assert [row["content"] for row in captured] == ["[[copy]]\nsame\n[[/copy]]"] * 2


@pytest.mark.asyncio
async def test_queued_copy_blocks_keep_distinct_ledger_rows_and_never_resend_delivered_parts(monkeypatch) -> None:
    import gateway.delivery_ledger as ledger

    runner = object.__new__(GatewayNotificationsMixin)
    adapter = object.__new__(_FakeAdapter)
    adapter.platform = "telegram"
    adapter._final_delivery_adapter = lambda _source: adapter
    adapter.gateway_runner = None
    rows = []
    sends = []
    attempts = {"copy": 0}
    monkeypatch.setattr(ledger, "ledger_enabled", lambda: True)
    monkeypatch.setattr(ledger, "record_obligation", lambda **kwargs: rows.append(kwargs))
    monkeypatch.setattr(ledger, "mark_attempting", lambda _oid: None)
    monkeypatch.setattr(ledger, "mark_delivered", lambda _oid: None)
    monkeypatch.setattr(ledger, "mark_failed", lambda _oid, _error: None)

    async def send_with_retry(*, chat_id, content, reply_to, metadata):
        sends.append((content, metadata.copy()))
        if metadata.get("copy_block"):
            attempts["copy"] += 1
            return SimpleNamespace(success=attempts["copy"] > 2, message_id=None, pre_send=True, error="blocked")
        return SimpleNamespace(success=True, message_id=None, pre_send=False, error=None)

    adapter._send_with_retry = send_with_retry

    async def _no_media(*args, **kwargs):
        return False

    runner._deliver_media_from_response = _no_media
    source = SimpleNamespace(chat_id="chat", platform="telegram", thread_id=None)
    response = "same\n[[copy]]\nsame\n[[/copy]]\n[[copy]]\nsame\n[[/copy]]"
    delivered = await runner._deliver_queued_first_response(
        response, source, adapter, event_message_id="reply", session_key="session", inbound_message_id="inbound")

    # The reply text landed, so the caller must not replay the whole response: the refused
    # blocks each hold their own failed ledger row and are redelivered from there.
    assert delivered is True
    # Block 1 was refused, so block 2 is held unsent behind it instead of overtaking it.
    assert [content for content, _metadata in sends] == ["same", "same"]
    copy_rows = [row for row in rows if row["content"] == "[[copy]]\nsame\n[[/copy]]"]
    assert len(copy_rows) == 2
    reply_rows = [row for row in rows if row["content"] == "same"]
    assert len({row["obligation_id"] for row in copy_rows + reply_rows}) == 3


@pytest.mark.asyncio
async def test_copy_blocks_propagate_ephemeral_ttl_and_delete() -> None:
    adapter = object.__new__(_FakeAdapter)
    deletes = []
    adapter._schedule_ephemeral_delete = lambda *args: deletes.append(args)

    async def send_final_ledgered(event, session_key, content, metadata, **kwargs):
        return SimpleNamespace(success=True, message_id=f"id-{content}"), adapter

    adapter.send_final_ledgered = send_final_ledgered
    await adapter._send_copy_blocks(
        SimpleNamespace(source=SimpleNamespace(chat_id="chat")), "session", ["one"], {}, lambda _r: None,
        is_ephemeral_response=True, ephemeral_ttl=7,
    )
    assert deletes == [("chat", "id-one", 7)]


def test_platforms_without_plain_copy_send_keep_blocks_inline() -> None:
    from gateway.copy_blocks import copy_free_text_for, split_copy_blocks_for
    text = "Intro\n[[copy]]\n**exact** text\n[[/copy]]\nAfter\n"
    telegram = SimpleNamespace(platform="telegram")
    discord = SimpleNamespace(platform="discord")
    assert split_copy_blocks_for(telegram, text) == ("Intro\nAfter\n", ["**exact** text"])
    assert split_copy_blocks_for(discord, text) == ("Intro\n**exact** text\nAfter\n", [])
    assert copy_free_text_for(telegram, text) == "Intro\nAfter\n"
    assert copy_free_text_for(discord, text) == "Intro\n**exact** text\nAfter\n"


@pytest.mark.asyncio
async def test_failed_copy_block_holds_later_blocks_in_order(monkeypatch) -> None:
    import gateway.delivery_ledger as ledger

    adapter = object.__new__(_FakeAdapter)
    adapter.platform = "telegram"
    adapter._final_delivery_adapter = lambda _source: adapter
    adapter.gateway_runner = None
    rows, failed, sends, results = [], [], [], []
    monkeypatch.setattr(ledger, "ledger_enabled", lambda: True)
    monkeypatch.setattr(ledger, "record_obligation", lambda **kwargs: rows.append(kwargs))
    monkeypatch.setattr(ledger, "mark_attempting", lambda _oid: None)
    monkeypatch.setattr(ledger, "mark_delivered", lambda _oid: None)
    monkeypatch.setattr(ledger, "mark_failed", lambda oid, _error: failed.append(oid))

    async def send_with_retry(*, chat_id, content, reply_to, metadata):
        sends.append(content)
        return SimpleNamespace(success=content != "two", message_id=None, pre_send=True, error="blocked")

    adapter._send_with_retry = send_with_retry
    source = SimpleNamespace(chat_id="chat", platform="telegram", thread_id=None)
    event = SimpleNamespace(source=source, message_id="m", ledger_message_id="m", text="")
    await adapter._send_copy_blocks(event, "session", ["one", "two", "three", "four"], {}, results.append)

    assert sends == ["one", "two"]
    assert [row["content"] for row in rows] == [f"[[copy]]\n{b}\n[[/copy]]" for b in ("one", "two", "three", "four")]
    assert len(failed) == 3 and len(set(failed)) == 3
    assert [getattr(r, "success", None) for r in results] == [True, False, False, False]


@pytest.mark.parametrize("body", ["\nlead", "trail\n", "\r\nboth\r\n", "lone cr\r", "plain"])
def test_ledger_wrapping_round_trips_body_bytes(body) -> None:
    from gateway.copy_blocks import wrap_copy_block
    assert extract_copy_blocks(wrap_copy_block(body)) == ("", [body])


def test_new_assistant_message_starts_on_a_new_line_for_copy_markers() -> None:
    stream = CopyMarkerStreamFilter(drop_bodies=True)
    out = stream.feed("Checking now.")
    stream.message_boundary()
    out += stream.feed("[[copy]]\nPASTE BODY\n[[/copy]]\nafter\n") + stream.flush()
    assert out == "Checking now.after\n"


@pytest.mark.asyncio
async def test_inline_copy_bodies_never_become_attachments(tmp_path) -> None:
    literal = tmp_path / "literal.txt"
    literal.write_text("x")
    adapter = object.__new__(_FakeAdapter)
    adapter.platform = "discord"
    event = SimpleNamespace(source=SimpleNamespace(chat_id="c", platform="discord"), text="")
    text = f"Intro\n[[copy]]\nMEDIA:{literal}\n[[/copy]]\n"
    extracted = await adapter._extract_response_content(text, event, "", is_ephemeral_response=True)
    assert extracted.media_files == [] and extracted.local_files == []
    assert extracted.copy_blocks == []
    assert extracted.text_content == f"Intro\nMEDIA:{literal}"


@pytest.mark.asyncio
async def test_queued_inline_copy_body_never_sends_an_attachment(tmp_path) -> None:
    literal = tmp_path / "literal.txt"
    literal.write_text("x")
    runner = object.__new__(GatewayNotificationsMixin)
    adapter = object.__new__(_FakeAdapter)
    adapter.platform = "discord"
    sends, media_texts = [], []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sends.append(content)
        return SimpleNamespace(success=True, message_id="m")

    async def media_from(response, *_args, **_kwargs):
        media_texts.append(response)
        return False

    adapter.send = send
    runner._deliver_media_from_response = media_from
    source = SimpleNamespace(chat_id="chat", platform="discord", thread_id=None)
    response = f"Intro\n[[copy]]\nMEDIA:{literal}\n[[/copy]]\n"
    assert await runner._deliver_queued_first_response(response, source, adapter, event_message_id="e")
    assert sends == [f"Intro\nMEDIA:{literal}"]
    assert all("MEDIA:" not in text for text in media_texts)


@pytest.mark.asyncio
async def test_api_commentary_renders_copy_blocks_inline() -> None:
    from gateway.platforms.api_server_openai_routes import _ResponsesStream
    stream = object.__new__(_ResponsesStream)
    stream.output_index, stream.emitted_items, written = 0, [], []

    async def _noop():
        return None

    async def _write(event, payload):
        written.append(payload)

    stream.close_reasoning_item, stream.write_event = _noop, _write
    await stream.emit_commentary("Note:\n[[copy]]\nexact\n[[/copy]]\n")
    assert "[[" not in repr(stream.emitted_items) and "[[" not in repr(written)
    assert "exact" in repr(stream.emitted_items)


@pytest.mark.asyncio
async def test_interrupted_turn_keeps_partial_block_inline_and_literal() -> None:
    runner = object.__new__(GatewayTurnMixin)
    adapter = object.__new__(_FakeAdapter)
    adapter.platform = "telegram"
    adapter._streaming_tts_turn_completed = lambda *_a, **_k: False
    runner._delivery_adapter_for = lambda _source: adapter
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    event = SimpleNamespace(source=SimpleNamespace(chat_id="chat", platform="telegram"), text="")
    text = "[[copy]]\n![paste this](https://example.com/literal.png)"
    returned = await runner._hmwa_deliver_turn_response(
        event, event.source, None, "session", None, {"interrupted": True}, [], text, None, False,
    )
    extracted = await adapter._extract_response_content(returned, event, "", is_ephemeral_response=True)
    assert extracted.copy_blocks == [] and extracted.images == []
    assert extracted.text_content == "![paste this](https://example.com/literal.png)"


def test_streaming_tts_never_speaks_copy_bodies() -> None:
    from gateway.streaming_tts_consumer import StreamingTTSConsumer
    consumer = object.__new__(StreamingTTSConsumer)
    consumer._aborted = consumer._finished = False
    consumer._streamer = object()
    spoken = []
    consumer._chunker = SimpleNamespace(feed=lambda t: [t], flush=lambda: [])
    consumer._enqueue_clauses = lambda clauses, *_a, **_k: spoken.extend(clauses)
    for delta in ["before\n[[copy]]\nPaste ", "exactly.\n[[/copy]]\nafter\n"]:
        consumer.on_delta(delta)
    assert "".join(spoken) == "before\nafter\n"


@pytest.mark.parametrize("body", ["```python\nprint(1)", "~~~\nopen [[/copy]]\n", "``` \nx\n```\ny\n```"])
def test_ledger_wrapping_round_trips_bodies_ending_in_an_open_fence(body) -> None:
    from gateway.copy_blocks import wrap_copy_block
    _, blocks = extract_copy_blocks("[[copy]]\n" + body)
    for extracted in blocks:
        assert extract_copy_blocks(wrap_copy_block(extracted)) == ("", [extracted])


@pytest.mark.asyncio
async def test_held_copy_blocks_are_ledgered_before_recovery_can_sweep(monkeypatch) -> None:
    import gateway.delivery_ledger as ledger

    adapter = object.__new__(_FakeAdapter)
    adapter.platform = "telegram"
    adapter._final_delivery_adapter = lambda _source: adapter
    adapter.gateway_runner = None
    rows, seen_at_finalize = [], []
    monkeypatch.setattr(ledger, "ledger_enabled", lambda: True)
    monkeypatch.setattr(ledger, "record_obligation", lambda **kwargs: rows.append(kwargs))
    monkeypatch.setattr(ledger, "mark_attempting", lambda _oid: None)
    monkeypatch.setattr(ledger, "mark_failed", lambda _oid, _error: None)

    async def finalize(obligation_id, result, event, delivery_adapter):
        # A recovered adapter sweeps right here: every follower must already be in the ledger.
        seen_at_finalize.append(len(rows))

    adapter._finalize_delivery_obligation = finalize

    async def send_with_retry(*, chat_id, content, reply_to, metadata):
        return SimpleNamespace(success=False, message_id=None, pre_send=True, error="send_path_degraded")

    adapter._send_with_retry = send_with_retry
    source = SimpleNamespace(chat_id="chat", platform="telegram", thread_id=None)
    event = SimpleNamespace(source=source, message_id="m", ledger_message_id="m", text="")
    results = []
    await adapter._send_copy_blocks(event, "session", ["one", "two", "three"], {}, results.append)
    assert seen_at_finalize == [3]
    assert len(results) == 3 and not any(r.success for r in results)
