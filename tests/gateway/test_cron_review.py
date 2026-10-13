"""Captured review turns execute through the adapter without transport effects."""
import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.cron_review import CronReviewControl
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.wake import admit_internal_event


class Adapter(BasePlatformAdapter):
    async def connect(self, **kwargs):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}

    async def send(self, chat_id, content, **kwargs):
        self.sent.append(content)
        return SendResult(success=True, message_id="sent")


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["PRESENT", "FIX\ncorrected report", RuntimeError("provider unavailable")])
async def test_internal_review_captures_reply_without_any_send(response):
    adapter = Adapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.sent = []
    adapter.send_typing = AsyncMock()
    adapter.set_message_handler(AsyncMock(
        side_effect=response if isinstance(response, Exception) else None,
        return_value=response if isinstance(response, str) else None))
    capture = Future()
    event = MessageEvent(text="review", source=SessionSource(
        platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", thread_id="139328"),
        internal=True, reply_expected=False, allow_gateway_control=False,
        metadata={"_cron_review_capture": capture})
    await admit_internal_event(adapter, event)
    if isinstance(response, Exception):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await asyncio.wrap_future(capture)
    else:
        assert await asyncio.wrap_future(capture) == response
    await asyncio.gather(*tuple(adapter._background_tasks))
    assert adapter.sent == []
    adapter.send_typing.assert_not_awaited()
    assert adapter._message_handler.await_count == 1


@pytest.mark.asyncio
async def test_busy_subject_is_refused_before_injection():
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    adapter = SimpleNamespace(_active_sessions={"key": object()}, _pending_messages={},
                              handle_message=AsyncMock())
    entry = SimpleNamespace(origin=source, session_id="sid")
    runner = SimpleNamespace(
        _plugin_injection_accepting=lambda: True,
        async_session_store=SimpleNamespace(lookup_by_session_key=AsyncMock(return_value=entry)),
        _restored_source=lambda entry: source,
        _plugin_injection_authorized=lambda *a, **kw: True,
        _delivery_adapter_for=lambda source: adapter)
    with pytest.raises(RuntimeError, match="busy"):
        await CronReviewControl(runner)._review("key", "sid", "review", 10)
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_control_socket_submission_runs_on_gateway_loop():
    import json
    from gateway.control_socket import GatewayControlServer
    control = CronReviewControl(SimpleNamespace())
    seen = []
    async def review(*args):
        seen.append(asyncio.get_running_loop())
        return "PRESENT"
    control._review = review
    server = GatewayControlServer(verb_handlers={"cron-review": control.submit,
                                               "cron-review-result": control.result})
    request = {"verb": "cron-review", "payload": {
        "session_key": "key", "session_id": "sid", "text": "report", "timeout_seconds": 10}}
    response = json.loads(await asyncio.to_thread(server.handle_request_line, json.dumps(request).encode()))
    assert response["ok"] is True
    token = response["result"]["token"]
    await asyncio.wrap_future(control.tasks[token][1])
    result = await asyncio.to_thread(control.result, {"token": token})
    assert result == {"status": "completed", "reply": "PRESENT"}
    assert seen == [asyncio.get_running_loop()]


def test_human_followup_is_preserved_outside_review_capture():
    from gateway.cron_review import defer_review_followup
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    human = MessageEvent(text="human follow-up", source=source)
    newer = MessageEvent(text="newer follow-up", source=source)
    queued = []
    runner = SimpleNamespace(_session_state=lambda key: SimpleNamespace(
        conversation=SimpleNamespace(queued_events=queued)))
    adapter = SimpleNamespace(_pending_messages={"key": newer})
    ctx = SimpleNamespace(event_metadata={"_cron_review_capture": Future()}, session_key="key")
    assert defer_review_followup(runner, ctx, adapter, human, human.text) == (None, None)
    assert adapter._pending_messages["key"] is human
    assert queued == [newer]
    assert "_cron_review_capture" not in human.metadata
    ctx.event_metadata = {}
    assert defer_review_followup(runner, ctx, adapter, human, human.text) == (human, human.text)


def test_bare_human_interrupt_is_rebuilt_as_a_reply_expected_human_event():
    from gateway.cron_review import defer_review_followup
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    ctx = SimpleNamespace(event_metadata={"_cron_review_capture": Future()}, session_key="key",
                          source=source, internal=True, reply_expected=False)
    adapter = SimpleNamespace(_pending_messages={})
    assert defer_review_followup(SimpleNamespace(), ctx, adapter, None, "human follow-up") == (None, None)
    event = adapter._pending_messages["key"]
    assert event.text == "human follow-up"
    assert event.source is source
    assert event.internal is False
    assert event.reply_expected is True
    assert "_cron_review_capture" not in event.metadata
