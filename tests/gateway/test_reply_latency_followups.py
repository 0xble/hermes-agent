"""Per-message clocks survive the real recursive, in-band follow-up drain."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig
from gateway.platforms.base import MessageEvent
from gateway.reply_latency import bind_agent_start, current_receipt, record_received
from gateway.run import GatewayRunner
from tests.gateway.test_queued_followup_processing_hooks import (
    SESSION_KEY, HookRecordingAdapter, _TwoTurnAgent, _install_fake_agent, _make_runner, _source,
)


def _records(caplog):
    return [json.loads(record.message.split(" ", 1)[1]) for record in caplog.records
            if record.message.startswith("gateway_reply_latency ")]


def _admission_runner(*, authorized=True):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, source: event)
    runner._is_user_authorized_for_source = lambda source: authorized
    runner._admit_bot_message_for_source = lambda source: True
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("pairing", [False, True])
async def test_unauthorized_receipt_is_stamped_but_not_logged(caplog, pairing):
    runner = _admission_runner(authorized=False)
    runner._get_unauthorized_dm_behavior = lambda platform, **kwargs: "pair" if pairing else "decline"
    runner._hm_offer_pairing_code = AsyncMock()
    runner._hm_send_unauthorized_decline = AsyncMock()
    source = _source()
    source.chat_type = "dm"
    source.user_id = "human"
    event = MessageEvent(text="unauthorized", source=source, message_id="denied")
    with caplog.at_level("INFO", logger="gateway.reply_latency"):
        assert await runner._hm_admit_event(event) is None
    assert event._attended_activity_received_at > 0
    assert not hasattr(event, "_reply_latency_receipt")
    assert _records(caplog) == []
    if pairing:
        runner._hm_offer_pairing_code.assert_awaited_once_with(source)
    else:
        runner._hm_send_unauthorized_decline.assert_awaited_once_with(source)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["identity", "emit"])
async def test_latency_failure_does_not_break_admission(monkeypatch, caplog, failure):
    def broken(*args, **kwargs):
        raise ValueError("diagnostics failed")

    if failure == "identity":
        monkeypatch.setattr("gateway.outbox.transport_id", broken)
    else:
        monkeypatch.setattr("gateway.reply_latency.logger.info", broken)
    runner = _admission_runner()
    event = MessageEvent(text="proceed", source=_source(), message_id="accepted")
    with caplog.at_level("DEBUG", logger="gateway.reply_latency"):
        assert await runner._hm_admit_event(event) == (event, event.source, False)
    assert event._attended_activity_received_at > 0
    runner._hm_pre_gateway_dispatch_hook.assert_awaited_once()
    assert "Could not" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("depth", [2, 3])
async def test_in_band_followup_owns_stream_and_terminal_final(
    monkeypatch, tmp_path, caplog, streaming, interrupted, depth,
):
    events = [MessageEvent(text=f"message-{n}", source=_source(), message_id=f"msg-{n}")
              for n in range(1, depth + 1)]
    adapter = HookRecordingAdapter()

    class ReplyAgent(_TwoTurnAgent):
        calls = []

        def run_conversation(self, *args, **kwargs):
            result = super().run_conversation(*args, **kwargs)
            n = len(self.calls)
            if n < depth:
                adapter._pending_messages[SESSION_KEY] = events[n]
            if n == 1 and interrupted:
                result.update(final_response="", interrupted=True)
            elif n > 1 and streaming:
                self.stream_delta_callback(result["final_response"])
            return result

    _install_fake_agent(monkeypatch, tmp_path, ReplyAgent)
    runner = _make_runner(adapter)
    runner.config.streaming = GatewayConfig().streaming
    runner.config.streaming.enabled = streaming
    with caplog.at_level("INFO", logger="gateway.reply_latency"):
        for event in events:
            record_received(event)
        # Only the chain head is bound here, just as at initial inbound admission.
        # The real _run_agent drain must bind every later event itself.
        bind_agent_start(events[0])
        head_receipt = current_receipt()
        result = await runner._run_agent(
            message=events[0].text, context_prompt="", history=[], source=_source(),
            session_id="latency-chain", session_key=SESSION_KEY,
            inbound_message_id=events[0].message_id,
        )
        assert current_receipt() is head_receipt
        assert result["queued_terminal_reply_receipt"] is events[-1]._reply_latency_receipt

        # Exercise the real outer handler's carrier, then its adapter final-send boundary.
        from tests.gateway.test_gateway_silence_tokens import _runner
        outer = _runner(monkeypatch, tmp_path)
        outer._run_agent = AsyncMock(return_value=result)
        await outer._handle_message_with_agent(events[0], _source(), SESSION_KEY, 1)
        assert events[0]._reply_latency_receipt is head_receipt
        assert events[0]._terminal_reply_latency_receipt is events[-1]._reply_latency_receipt
        await adapter.send_final_ledgered(
            events[0], SESSION_KEY, result["final_response"], {}, reply_to=None,
        )

    assert ReplyAgent.calls == [event.text for event in events]
    records = _records(caplog)
    assert [record["message_id"] for record in records if record["event"] == "agent_start"] == [
        event.message_id for event in events]
    replies = [record["message_id"] for record in records if record["event"] == "first_reply"]
    assert replies == [event.message_id for event in events[(1 if interrupted else 0):]]
    assert len(replies) == len(set(replies))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_in_band_followup_restores_head_on_failure(monkeypatch, tmp_path, failure):
    class BrokenAgent(_TwoTurnAgent):
        calls = []

        def run_conversation(self, *args, **kwargs):
            result = super().run_conversation(*args, **kwargs)
            if len(self.calls) > 1:
                raise failure("follow-up failed")
            return result

    _install_fake_agent(monkeypatch, tmp_path, BrokenAgent)
    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)
    head = MessageEvent(text="head", source=_source(), message_id="head")
    followup = MessageEvent(text="followup", source=_source(), message_id="followup")
    adapter._pending_messages[SESSION_KEY] = followup
    bind_agent_start(head)
    head_receipt = current_receipt()
    with pytest.raises(failure):
        await runner._run_agent(message=head.text, context_prompt="", history=[], source=_source(),
                                session_id="latency-failure", session_key=SESSION_KEY)
    assert current_receipt() is head_receipt
    assert followup._reply_latency_receipt.started


@pytest.mark.asyncio
async def test_eventless_interrupt_does_not_borrow_head_receipt(monkeypatch, tmp_path, caplog):
    class EventlessAgent(_TwoTurnAgent):
        calls = []
        receipts = []

        def run_conversation(self, *args, **kwargs):
            self.receipts.append(current_receipt())
            result = super().run_conversation(*args, **kwargs)
            if len(self.calls) == 1:
                result.update(interrupted=True, interrupt_message="eventless follow-up", final_response="")
            return result

    _install_fake_agent(monkeypatch, tmp_path, EventlessAgent)
    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)
    head = MessageEvent(text="head", source=_source(), message_id="head")
    with caplog.at_level("INFO", logger="gateway.reply_latency"):
        bind_agent_start(head)
        receipt = current_receipt()
        result = await runner._run_agent(message=head.text, context_prompt="", history=[], source=_source(),
                                         session_id="latency-eventless", session_key=SESSION_KEY)
    assert EventlessAgent.calls == ["head", "eventless follow-up"]
    assert EventlessAgent.receipts == [receipt, None]
    assert result["queued_terminal_reply_receipt"] is None
    assert current_receipt() is receipt
    assert not [record for record in _records(caplog) if record["event"] == "first_reply"]
