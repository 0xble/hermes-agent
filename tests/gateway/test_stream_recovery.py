"""Durable real-preview ownership and fencing at crash/resume boundaries."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway import delivery_ledger as ledger
from gateway import stream_recovery as recovery
from gateway.platforms.base import SendResult
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


@pytest.fixture(autouse=True)
def isolated_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")


def record(**changes):
    values = dict(turn_id="old", session_key="key", session_id="sid", content="visible", message_id="m1")
    values.update(changes)
    recovery.record_preview(**values)


def test_snapshot_is_durable_and_fenced():
    record()
    assert recovery.load_preview("key", "sid", "old") == dict(content="visible", message_id="m1")
    assert recovery.load_preview("other", "sid", "old") is None
    assert recovery.load_preview("key", "other", "old") is None
    assert recovery.load_preview("key", "sid", "successor") is None
    record(content="complete intended edit")
    assert recovery.load_preview("key", "sid", "old")["content"] == "complete intended edit"
    recovery.discard_preview("old")
    assert recovery.load_preview("key", "sid", "old") is None


@pytest.mark.asyncio
async def test_first_send_records_before_transport_and_id_after_ack():
    gate = asyncio.Event()
    called = asyncio.Event()

    async def journal(content, message_id):
        record(content=content, message_id=message_id)

    async def send(**kwargs):
        assert recovery.load_preview("key", "sid", "old") == dict(content="answer", message_id=None)
        called.set()
        await gate.wait()
        return SendResult(success=True, message_id="accepted-id")

    adapter = SimpleNamespace(send=send)
    consumer = GatewayStreamConsumer(adapter, "chat", config=StreamConsumerConfig(cursor=" ▉"), on_preview=journal)
    task = asyncio.create_task(consumer._first_send("answer ▉", finalize=False))
    await called.wait()
    assert recovery.load_preview("key", "sid", "old")["message_id"] is None
    gate.set()
    assert await task
    assert recovery.load_preview("key", "sid", "old") == dict(content="answer", message_id="accepted-id")


@pytest.mark.asyncio
async def test_edit_write_ahead_retains_known_platform_id():
    async def journal(content, message_id):
        record(content=content, message_id=message_id)

    async def edit(**kwargs):
        assert recovery.load_preview("key", "sid", "old") == dict(content="complete answer", message_id="m1")
        return SendResult(success=True, message_id="m1")

    adapter = SimpleNamespace(edit_message=edit)
    consumer = GatewayStreamConsumer(adapter, "chat", config=StreamConsumerConfig(cursor=""), on_preview=journal)
    consumer._message_id = "m1"
    consumer._last_sent_text = "partial"
    assert await consumer._edit_existing("complete answer", finalize=False, is_turn_final=False)


@pytest.mark.parametrize("pending,session_id,token,restored", [
    (True, "sid", "old", True), (False, "sid", "old", False),
    (True, "reset-sid", "old", False), (True, "sid", "successor", False),
])
def test_resume_reuses_only_exact_predecessor(pending, session_id, token, restored):
    record()
    entry = SimpleNamespace(resume_pending=pending, resume_turn_id=token, session_id=session_id)
    store = SimpleNamespace(_lock=threading.RLock(), _ensure_loaded_locked=lambda: None, _entries={"key": entry})
    runner = SimpleNamespace(session_store=store)
    ctx = SimpleNamespace(session_key="key", session_id="sid")
    consumer = GatewayStreamConsumer(SimpleNamespace(), "chat")
    recovery.restore_preview(runner, ctx, consumer)
    assert consumer.message_id == ("m1" if restored else None)
    assert consumer._already_sent is restored
    assert consumer._recovered_send_uncertain is False
    assert consumer.cfg.buffer_only is False


def test_ack_lost_first_send_does_not_invent_platform_id():
    record(message_id=None)
    entry = SimpleNamespace(resume_pending=True, resume_turn_id="old", session_id="sid")
    store = SimpleNamespace(_lock=threading.RLock(), _ensure_loaded_locked=lambda: None, _entries={"key": entry})
    consumer = GatewayStreamConsumer(SimpleNamespace(), "chat")
    recovery.restore_preview(SimpleNamespace(session_store=store), SimpleNamespace(session_key="key", session_id="sid"), consumer)
    assert consumer.message_id is None
    assert consumer._already_sent is False
    assert consumer._recovered_send_uncertain is True
    assert consumer.cfg.buffer_only is True
    messages = [{"role": "assistant", "content": "real re-answer"}]
    result = {"final_response": "real re-answer", "completed": True, "messages": messages}
    recovery.mark_recovered_reply(result, consumer)
    assert result["final_response"] == ledger.RECOVERED_MARKER + "real re-answer"
    assert messages == [{"role": "assistant", "content": "real re-answer"}]
    for flags in ({"failed": True}, {"interrupted": True}, {"completed": False}):
        unfinished = {"final_response": "diagnostic", **flags}
        recovery.mark_recovered_reply(unfinished, consumer)
        assert unfinished["final_response"] == "diagnostic"
    ordinary = {"final_response": "ordinary", "completed": True}
    recovery.mark_recovered_reply(ordinary, GatewayStreamConsumer(SimpleNamespace(), "chat"))
    assert ordinary["final_response"] == "ordinary"
    recovery.mark_recovered_reply(result, consumer)
    assert result["final_response"] == ledger.RECOVERED_MARKER + "real re-answer"
