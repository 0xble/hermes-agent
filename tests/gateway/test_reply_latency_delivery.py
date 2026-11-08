"""Confirmed progress and final deliveries share the original inbound identity."""

from types import SimpleNamespace

import pytest

from gateway.reply_latency import bind_agent_start, record_received
from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from tests.gateway.test_reply_latency_logs import Adapter, event, records


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["progress_send", "progress_edit", "status", "text_send", "text_edit"])
async def test_first_visible_delivery_survives_admission_and_later_chunks(caplog, monkeypatch, first):
    caplog.set_level("INFO", logger="gateway.reply_latency")
    clock = [1800000000.0]
    monkeypatch.setattr("gateway.reply_latency.time", SimpleNamespace(time=lambda: clock[0]))
    adapter = Adapter()
    incoming = event(adapter, "inbound", "topic")
    record_received(incoming)
    # The real FIFO codec reconstructs an event and changes transport turn identity.
    from gateway.session_envelope import snapshot_native, restore_native
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=False),
        session_store=SimpleNamespace(_generate_session_key=lambda source: "route"),
        _adapter_for_source=lambda source: adapter,
        _adapter_flag=lambda *args: False,
        _adapter_profile_for_source=lambda source: None,
        _is_user_authorized_for_source=lambda source, **kwargs: True,
        _delivery_adapter_for=lambda source: adapter,
    )
    restored = restore_native(snapshot_native(runner, incoming), runner)
    restored._outbox_admission_id = "admission:changed:1"
    clock[0] += 1
    bind_agent_start(restored)
    ctx = TurnContext(source=restored.source, _progress_metadata={}, _progress_reply_to=None,
                      _run_still_current=lambda: True, _cleanup_progress=False)
    turn = TurnRunner(runner, ctx)
    st = turn._progress_edit_state(adapter)
    # Another turn on the same chat must not steal a scheduled progress callback's receipt.
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    stream = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig(cursor=""))
    bind_agent_start(event(adapter, "other", "other-topic"))
    actions = {
        "progress_send": lambda: turn._send_progress_text(st, "Working"),
        "progress_edit": lambda: turn._edit_progress_message(st, "card", "Working"),
        "status": lambda: turn._status_delivery.send(adapter, "chat", "thinking", "Working", {}),
        "text_send": lambda: stream._first_send("Answer", finalize=False),
        "text_edit": lambda: stream._edit_message(message_id="bubble", content="Answer"),
    }
    adapter.ok = False
    await actions[first]()
    assert not [r for r in records(caplog) if r["event"] == "first_reply"]
    adapter.ok = True
    clock[0] += 2
    await actions[first]()
    visible = [r for r in records(caplog) if r["event"] == "first_reply"]
    assert len(visible) == 1
    expected_kind = "progress_send" if first == "status" else first
    assert visible[0].get("delivery_kind") == expected_kind
    assert visible[0]["message_id"] == "inbound"
    clock[0] += 5
    await adapter.send_final_ledgered(restored, "route", "Last chunk", {}, reply_to=None)
    from gateway.reply_latency_report import read_records
    rows = read_records()
    row = next(r for r in rows if r["message_id"] == "inbound")
    assert row["received"] == 1800000000.0
    assert row["agent_start"] == 1800000001.0
    assert row["first_visible"] == 1800000003.0
    assert row["first_visible_kind"] == expected_kind
    assert row["final_delivery"] == 1800000008.0
    assert len([r for r in rows if r["message_id"] == "inbound"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["failed", "skipped", "deferred"])
async def test_failed_or_skipped_final_is_not_a_delivery(caplog, monkeypatch, refusal):
    caplog.set_level("INFO", logger="gateway.reply_latency")
    adapter = Adapter()
    incoming = event(adapter, "failed")
    bind_agent_start(incoming)
    failed_send = adapter.send
    async def refused(*args, **kwargs):
        return SendResult(success=refusal != "failed", deferred=refusal == "deferred",
                          raw_response={"skipped": True} if refusal == "skipped" else None,
                          error="forbidden" if refusal == "failed" else None,
                          error_kind="forbidden" if refusal == "failed" else None)
    monkeypatch.setattr(adapter, "send", refused)
    await adapter.send_final_ledgered(incoming, "route", "Answer", {}, reply_to=None)
    assert not [r for r in records(caplog) if r["event"] in {"first_reply", "final_delivery"}]
    monkeypatch.setattr(adapter, "send", failed_send)
    await adapter.send_final_ledgered(incoming, "route", "Answer", {}, reply_to=None)
    assert len([r for r in records(caplog) if r["event"] == "final_delivery"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_tail", [False, True])
async def test_telegram_first_chunk_and_last_chunk_are_confirmations_not_return_time(caplog, monkeypatch, fail_tail):
    from tests.gateway.test_telegram_progress_literals import _telegram

    caplog.set_level("INFO", logger="gateway.reply_latency")
    clock = [1800000000.0]
    monkeypatch.setattr("gateway.reply_latency.time", SimpleNamespace(time=lambda: clock[0]))
    adapter = _telegram("never")
    incoming = event(adapter, "split", "77")
    bind_agent_start(incoming)

    async def chunk(chat_id, text, **kwargs):
        clock[0] += 10
        if fail_tail and clock[0] > 1800000010:
            from telegram.error import Forbidden
            raise Forbidden("forbidden")
        return SimpleNamespace(message_id=str(clock[0]))

    monkeypatch.setattr(adapter._bot, 'send_message', chunk)
    await adapter.send_final_ledgered(incoming, "route", "word " * 1500, {"thread_id": "77"}, reply_to=None)
    first = [r for r in records(caplog) if r["event"] == "first_reply"]
    assert first and first[0]["timestamp"] == 1800000010.0
    from gateway.reply_latency_report import read_records
    row, = read_records()
    assert row["first_visible"] == 1800000010.0
    assert row.get("final_delivery") == (None if fail_tail else clock[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fresh", "split", "fallback"])
async def test_streamed_final_records_last_confirmed_chunk(monkeypatch, mode):
    import time
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    from gateway.reply_latency_report import read_records

    adapter = Adapter()
    if mode != "fresh":
        adapter.MAX_MESSAGE_LENGTH = 64
    clock = [1800000000.0]
    monkeypatch.setattr("gateway.reply_latency.time", SimpleNamespace(time=lambda: clock[0]))
    confirmations = []
    original_send = adapter.send

    async def confirmed_send(*args, **kwargs):
        clock[0] += 10
        result = await original_send(*args, **kwargs)
        confirmations.append(clock[0])
        result.message_id = f"chunk-{len(confirmations)}"
        result.raw_response = {"reply_latency_confirmed_at": clock[0]}
        clock[0] += 1  # Bookkeeping after confirmation must not move the delivery clock.
        return result

    monkeypatch.setattr(adapter, "send", confirmed_send)
    incoming = event(adapter, "stream")
    bind_agent_start(incoming)
    consumer = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig(
        cursor="", fresh_final_after_seconds=.001 if mode == "fresh" else 0))
    consumer._message_id = "preview"
    consumer._message_created_ts = time.monotonic() - 10
    consumer._already_sent = True
    if mode == "fallback":
        consumer._fallback_final_send = True
        consumer._edit_supported = False
    consumer.finish("The complete answer. " * (100 if mode == "fallback" else 20 if mode == "split" else 1))
    await consumer.run()
    row, = read_records()
    assert row["final_delivery"] == 1800000000.0 + 11 * len(confirmations) - 1
    assert row["final_delivery"] == confirmations[-1]
    assert len(confirmations) > (0 if mode == "fresh" else 1)
