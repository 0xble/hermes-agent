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


def test_private_review_response_prefers_raw_model_response_over_display_response():
    from gateway.cron_review import private_turn_response
    event = SimpleNamespace(internal=True, metadata={"_cron_review_capture": Future()},
                            _agent_final_response="PRESENT")
    assert private_turn_response(event, {"final_response": "PRESENT\nextra"}, "display") == (True, "PRESENT")


def test_private_review_response_preserves_explicit_empty_raw_model_response():
    from gateway.cron_review import private_turn_response
    event = SimpleNamespace(internal=True, metadata={"_cron_review_capture": Future()},
                            _agent_final_response="")
    assert private_turn_response(event, {"final_response": "normalized"}, "display") == (True, "")


@pytest.mark.asyncio
async def test_busy_subject_is_refused_before_injection(monkeypatch):
    import gateway.cron_review as cron_review
    monkeypatch.setattr(cron_review, "_IDLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(cron_review, "_MIN_REVIEW_SECONDS", 0.02)
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
        await CronReviewControl(runner)._review("key", "sid", "review", 0.05)
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_waits_for_a_busy_subject_to_go_idle(monkeypatch):
    import gateway.cron_review as cron_review
    monkeypatch.setattr(cron_review, "_IDLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(cron_review, "_MIN_REVIEW_SECONDS", 0.02)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    adapter = SimpleNamespace(_active_sessions={"key": object()}, _pending_messages={})
    entry = SimpleNamespace(origin=source, session_id="sid")
    admitted = []

    async def admit(adapter_, event):
        assert "key" not in adapter._active_sessions
        admitted.append(event)
        event.metadata["_cron_review_capture"].set_result("PRESENT")

    monkeypatch.setattr("gateway.wake.admit_internal_event", admit)

    class Scope:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    runner = SimpleNamespace(
        _plugin_injection_accepting=lambda: True,
        async_session_store=SimpleNamespace(lookup_by_session_key=AsyncMock(return_value=entry)),
        _restored_source=lambda entry: source,
        _plugin_injection_authorized=lambda *a, **kw: True,
        _delivery_adapter_for=lambda source: adapter,
        _plugin_injection_scope=lambda source: Scope())
    asyncio.get_running_loop().call_later(0.05, adapter._active_sessions.clear)
    assert await CronReviewControl(runner)._review("key", "sid", "review", 5) == "PRESENT"
    assert len(admitted) == 1
    assert runner.async_session_store.lookup_by_session_key.await_count == 2


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


@pytest.mark.asyncio
async def test_waited_review_is_refused_once_the_gateway_starts_draining(monkeypatch):
    import gateway.cron_review as cron_review
    monkeypatch.setattr(cron_review, "_IDLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(cron_review, "_MIN_REVIEW_SECONDS", 0.02)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    adapter = SimpleNamespace(_active_sessions={"key": object()}, _pending_messages={})
    entry = SimpleNamespace(origin=source, session_id="sid")
    accepting = [True]
    admit = AsyncMock()
    monkeypatch.setattr("gateway.wake.admit_internal_event", admit)
    runner = SimpleNamespace(
        _plugin_injection_accepting=lambda: accepting[0],
        async_session_store=SimpleNamespace(lookup_by_session_key=AsyncMock(return_value=entry)),
        _restored_source=lambda entry: source,
        _plugin_injection_authorized=lambda *a, **kw: True,
        _delivery_adapter_for=lambda source: adapter)

    def drain():  # a drain is what idles the busy topic
        accepting[0] = False
        adapter._active_sessions.clear()

    asyncio.get_running_loop().call_later(0.05, drain)
    with pytest.raises(RuntimeError, match="not accepting"):
        await CronReviewControl(runner)._review("key", "sid", "review", 5)
    admit.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_reaching_a_busy_session_fails_its_capture_and_never_queues():
    adapter = Adapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.sent = []
    adapter.set_message_handler(AsyncMock(return_value="PRESENT"))
    busy_handler = AsyncMock(return_value=True)
    adapter._busy_session_handler = busy_handler
    capture = Future()
    event = MessageEvent(text="review", source=SessionSource(
        platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", thread_id="139328"),
        internal=True, reply_expected=False, allow_gateway_control=False,
        metadata={"_cron_review_capture": capture})
    await adapter._handle_message_while_active(event, "key")
    assert event._gateway_accepted is True  # the caller sees the busy cause, not a wake refusal
    with pytest.raises(RuntimeError, match="busy"):
        capture.result(timeout=0)
    busy_handler.assert_not_awaited()
    adapter._message_handler.assert_not_awaited()
    assert "key" not in adapter._pending_messages
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_runner_running_session_fast_path_fails_a_review_instead_of_interrupting():
    from gateway.run_inbound import GatewayInboundMixin
    capture = Future()
    event = MessageEvent(text="review", source=SessionSource(
        platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", thread_id="139328"),
        internal=True, reply_expected=False, allow_gateway_control=False,
        metadata={"_cron_review_capture": capture})
    runner = SimpleNamespace(_draining=False)  # any further attribute access would fail the test
    result = await GatewayInboundMixin._hm_handle_running_session_message(
        runner, event, event.source, "key")
    assert result is None
    with pytest.raises(RuntimeError, match="busy"):
        capture.result(timeout=0)


def test_drain_never_preserves_a_review():
    from gateway.run_busy import GatewayBusySessionMixin
    capture = Future()
    event = MessageEvent(text="review", source=SessionSource(
        platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", thread_id="139328"),
        internal=True, reply_expected=False, allow_gateway_control=False,
        metadata={"_cron_review_capture": capture})
    runner = SimpleNamespace()  # any queue or spool access would fail the test
    assert GatewayBusySessionMixin._preserve_drain_event(runner, "key", event) is False
    with pytest.raises(RuntimeError, match="busy"):
        capture.result(timeout=0)


@pytest.mark.asyncio
async def test_muted_review_turn_still_reaches_the_scheduler_as_its_exact_reply(monkeypatch):
    """A review turn mutes its own presentation; that mute must not erase the captured reply.

    Drives control socket -> CronReviewControl._review -> admit_internal_event -> adapter ->
    the runner's real private delivery step, then parses exactly what the scheduler receives.
    """
    from cron.review import parse_decision
    from gateway.run_turn import GatewayTurnMixin

    adapter = Adapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.sent = []
    adapter.send_typing = AsyncMock()

    async def runner_turn(event):
        # The runner forces mute_notification_reply for every review turn and carries it back
        # onto the event, exactly as _run_agent/_handle_message_with_agent do.
        agent_result = {"final_response": "PRESENT", "_model_final_response": "PRESENT",
                        "_notification_reply_muted": True}
        event._notification_reply_muted = agent_result["_notification_reply_muted"]
        return await GatewayTurnMixin._hmwa_deliver_turn_response(
            SimpleNamespace(), event, event.source, None, event.metadata["gateway_session_key"], 1,
            agent_result, [], "PRESENT", None, False)

    adapter.set_message_handler(runner_turn)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm",
                           thread_id="139328")
    entry = SimpleNamespace(origin=source, session_id="sid")

    class Scope:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    runner = SimpleNamespace(
        _plugin_injection_accepting=lambda: True,
        async_session_store=SimpleNamespace(lookup_by_session_key=AsyncMock(return_value=entry)),
        _restored_source=lambda entry: source,
        _plugin_injection_authorized=lambda *a, **kw: True,
        _delivery_adapter_for=lambda source: adapter,
        _plugin_injection_scope=lambda source: Scope())
    key = "agent:main:telegram:dm:chat:139328"
    reply = await CronReviewControl(runner)._review(key, "sid", "review", 5)
    await asyncio.gather(*tuple(adapter._background_tasks))
    assert reply == "PRESENT"
    assert parse_decision(reply) == ("PRESENT", "")
    assert adapter.sent == []
    adapter.send_typing.assert_not_awaited()
