from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from gateway.copy_blocks import CopyMarkerStreamFilter, extract_copy_blocks, render_copy_blocks_inline
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
    assert render_copy_blocks_inline("before\n[[copy]]\nbody\n[[/copy]]") == "before\n\nbody"


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
async def test_queued_copy_blocks_keep_distinct_ledger_rows_and_retry_together(monkeypatch) -> None:
    import gateway.delivery_ledger as ledger

    runner = object.__new__(GatewayNotificationsMixin)
    adapter = object.__new__(_FakeAdapter)
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
    first = await runner._deliver_queued_first_response(
        response, source, adapter, event_message_id="reply", session_key="session", inbound_message_id="inbound")
    second = await runner._deliver_queued_first_response(
        response, source, adapter, event_message_id="reply", session_key="session", inbound_message_id="inbound")

    assert first is False
    assert second is True
    copy_sends = [item for item in sends if item[1].get("copy_block")]
    assert [content for content, _metadata in copy_sends] == ["same", "same", "same", "same"]
    copy_rows = [row for row in rows if row["content"] == "[[copy]]\nsame\n[[/copy]]"]
    assert len(copy_rows) == 4
    assert len({row["obligation_id"] for row in copy_rows}) == 2
    assert all(copy_rows.count(row) == 2 for row in copy_rows[:2])


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
