"""Receipt-to-confirmed-text logs do not borrow another message's clock."""

import json

import pytest

from gateway.platforms.base import BasePlatformAdapter, Platform, PlatformConfig, SendResult
from gateway.platforms.event import MessageEvent
from gateway.reply_latency import bind_agent_start, record_received
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


class Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)
        self.ok = True

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=self.ok, message_id="sent" if self.ok else None,
                          error=None if self.ok else "forbidden", error_kind=None if self.ok else "forbidden")

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        return await self.send(chat_id, content)

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


def records(caplog):
    return [json.loads(r.message.split("gateway_reply_latency ", 1)[1])
            for r in caplog.records if "gateway_reply_latency " in r.message]


def event(adapter, message_id, thread_id=None):
    source = adapter.build_source(chat_id="chat", user_id="human")
    source.thread_id = thread_id
    return MessageEvent(text="question", source=source, message_id=message_id)


@pytest.mark.parametrize("transport", ["send", "draft", "native", "edit", "fallback"])
@pytest.mark.asyncio
async def test_receipt_is_once_and_only_confirmed_text_ends_its_own_turn(caplog, transport):
    caplog.set_level("INFO", logger="gateway.reply_latency")
    adapter = Adapter()
    first, second = event(adapter, "one", "a"), event(adapter, "two", "b")
    first._attended_activity_received_at = 1700000000.0
    record_received(first)
    record_received(first)
    bind_agent_start(first)
    consumer = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig(cursor=""))
    record_received(second)
    bind_agent_start(second)
    later = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig(cursor=""))
    assert await consumer._send_or_edit("   ")
    adapter.ok = False
    assert not await consumer._send_or_edit("not delivered")
    assert not [r for r in records(caplog) if r["event"] == "first_reply"]
    adapter.ok = True
    # Exercise the real success boundaries across each transport.
    async def draft(chat_id, draft_id, content, metadata=None):
        return SendResult(success=True)

    async def frame(*args, **kwargs):
        return True

    adapter.send_draft = draft
    adapter.send_stream_frame = frame
    consumer._draft_id = 42
    consumer._native_stream_opened = True
    methods = {
        "send": ("_first_send", {"text": "confirmed answer", "finalize": True}),
        "draft": ("_send_draft_frame", {"text": "confirmed answer"}),
        "native": ("_native_push", {"text": "confirmed answer", "finalize": False, "is_turn_final": False}),
        "edit": ("_edit_message", {"message_id": "adopted", "content": "confirmed answer"}),
        "fallback": ("_send_with_flood_retry", {"content": "confirmed answer", "retry_log": "retry %s"}),
    }
    method, kwargs = methods[transport]
    await getattr(consumer, method)(**kwargs)
    assert await later._send_or_edit("other answer", finalize=True)
    await adapter.send_final_ledgered(first, "session", "final answer", {}, reply_to=None)
    logs = records(caplog)
    assert [r["event"] for r in logs] == ["received", "agent_start", "received", "agent_start",
                                             "first_reply", "first_reply"]
    assert [(r["message_id"], r["thread_id"]) for r in logs if r["event"] == "first_reply"] == [
        ("one", "a"), ("two", "b")]
    assert logs[0]["timestamp"] == 1700000000.0
    assert logs[0]["turn_id"] == "chat:chat:message:one"
    assert all(r["origin"] == "user" and r["platform"] == "telegram" for r in logs)
    assert all("question" not in json.dumps(r) and "answer" not in json.dumps(r) for r in logs)


@pytest.mark.asyncio
async def test_final_send_failure_and_internal_turn_never_stop_human_receipt(caplog):
    caplog.set_level("INFO", logger="gateway.reply_latency")
    adapter = Adapter()
    human = event(adapter, "human")
    record_received(human)
    bind_agent_start(human)
    internal = event(adapter, "wake")
    internal.internal = True
    record_received(internal)
    bind_agent_start(internal)
    notice = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig(cursor=""))
    assert await notice._send_or_edit("background text", finalize=True)
    adapter.ok = False
    await adapter.send_final_ledgered(human, "session", "answer", {}, reply_to=None)
    assert [r["event"] for r in records(caplog)] == ["received", "agent_start"]
    adapter.ok = True
    await adapter.send_final_ledgered(human, "session", "answer", {}, reply_to=None)
    assert [r["event"] for r in records(caplog)] == ["received", "agent_start", "first_reply"]


@pytest.mark.asyncio
async def test_seed_frames_skipped_sends_and_failed_drafts_are_not_visible_text(caplog):
    caplog.set_level("INFO", logger="gateway.reply_latency")
    adapter = Adapter()
    human = event(adapter, "not-yet")
    record_received(human)
    bind_agent_start(human)
    consumer = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig(cursor="cursor"))

    async def frame(*args, **kwargs):
        return True

    async def draft(*args, **kwargs):
        return SendResult(success=False, error="failed")

    async def skipped(*args, **kwargs):
        return SendResult(success=True, raw_response={"skipped": True})

    adapter.send_stream_frame = frame
    adapter.send_draft = draft
    assert await consumer._try_seed_frame("seed failed: %s")
    assert await consumer._native_push("cursor", finalize=False, is_turn_final=False)
    assert not await consumer._send_draft_frame("not yet")
    adapter.edit_message = skipped
    await consumer._edit_message(message_id="adopted", content="not yet")
    adapter.send = skipped
    await adapter.send_final_ledgered(human, "session", "not yet", {}, reply_to=None)
    assert [r["event"] for r in records(caplog)] == ["received", "agent_start"]


@pytest.mark.parametrize("kind", ["internal", "bot", "reaction", "legacy_recovery"])
def test_synthetic_receipts_are_excluded(caplog, kind):
    caplog.set_level("INFO", logger="gateway.reply_latency")
    adapter = Adapter()
    synthetic = event(adapter, kind)
    mutate = {
        "internal": lambda: setattr(synthetic, "internal", True),
        "bot": lambda: setattr(synthetic.source, "is_bot", True),
        "reaction": lambda: synthetic.metadata.update({"reaction": True}),
        "legacy_recovery": lambda: setattr(synthetic, "_hermes_recovered_followup", True),
    }
    mutate[kind]()
    assert record_received(synthetic) is None
    bind_agent_start(synthetic)
    assert records(caplog) == []
