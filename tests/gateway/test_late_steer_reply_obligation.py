"""A buffered steer belongs to its successor unless a live turn actually incorporates it."""

import threading
from unittest.mock import AsyncMock

import pytest

from agent.interrupt_control import InterruptControlMixin
from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


class Receiver(InterruptControlMixin):
    _supports_active_turn_redirect = True

    def __init__(self):
        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()
        self._pending_redirect_lock = threading.Lock()
        self._model_request_active = threading.Event()
        self._executing_tools = False
        self._active_children = []
        self._active_children_lock = threading.Lock()
        self._turn_reply_expected = False


def _running():
    runner = GatewayRunner(GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1", chat_type="dm")
    agent = Receiver()
    opener = MessageEvent(text="[relay from=agent@example.com receipt=r1]\nFYI", source=source,
                          reply_expected=False)
    ctx = TurnContext(session_key="key", source=source, reply_expected=False,
                      session_id="session", history=[])
    turn = runner._session_state("key").turn
    turn.agent, turn.event, turn.ctx = agent, opener, ctx
    runner._deliver_queued_first_response = AsyncMock(return_value=True)
    runner._delivery_adapter_for = lambda _source: None
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._run_agent = AsyncMock(return_value={"final_response": "Acknowledged.", "messages": []})
    return runner, agent, opener, ctx, source


def _incorporate(agent):
    messages = [{"role": "tool", "content": "done", "tool_call_id": "t1"}]
    apply_pending_steer_to_tool_results(agent, messages, 1)
    assert messages[-1]["role"] == "user"
    return messages[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier_human", [False, True], ids=["late-only", "mixed"])
async def test_leftover_steer_does_not_change_predecessor_silence_contract(earlier_human):
    runner, agent, opener, ctx, source = _running()
    if earlier_human:
        earlier = MessageEvent(text="Check the earlier request", source=source)
        outcome = await runner._resolve_busy_steer_or_redirect(earlier, "key", "steer", agent)
        assert outcome.steered
        _incorporate(agent)
        assert (opener.reply_expected, ctx.reply_expected, agent._turn_reply_expected) == (None, None, None)

    # The model has finished. redirect() declines, steer() accepts, and no tool/API boundary remains.
    incoming = MessageEvent(text="OK", source=source)
    outcome = await runner._resolve_busy_steer_or_redirect(incoming, "key", "steer", agent, preempt=True)
    assert outcome.steered and not outcome.redirected
    result = {"final_response": "NO_REPLY", "messages": [], "pending_steer": agent._drain_leftover_steer()}
    adapter = type("Adapter", (), {"_pending_messages": {}, "get_pending_message": lambda self, key: None})()
    pending_event, pending = await runner._run_agent_drain_pending(result, adapter, source, "key")
    assert pending_event is None and pending == runner._steer_text_with_origin("OK", incoming)

    await runner._run_agent_queued_followup(ctx, adapter, pending, pending_event, result, result, None)
    if earlier_human:
        runner._deliver_queued_first_response.assert_awaited_once()
        assert "No reply was written" in runner._deliver_queued_first_response.call_args.args[0]
    else:
        runner._deliver_queued_first_response.assert_not_awaited()
        assert (opener.reply_expected, ctx.reply_expected, agent._turn_reply_expected) == (False, False, False)
    runner._run_agent.assert_awaited_once()
    successor = runner._run_agent.call_args.kwargs
    assert successor["message"] == pending
    assert successor["message"].splitlines().count("OK") == 1
    assert successor["reply_expected"] is None
    assert successor["persist_user_display_kind"] != "internal_notification"
    assert agent._drain_leftover_steer() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["busy", "priority", "slash", "tool-redirect"])
async def test_incorporated_steer_promotes_all_reply_contract_readers(route):
    runner, agent, opener, ctx, source = _running()
    event = MessageEvent(text="Please answer", source=source, reply_expected=True)
    if route == "busy":
        outcome = await runner._resolve_busy_steer_or_redirect(event, "key", "steer", agent)
        assert outcome.steered
    elif route == "priority":
        runner._hm_busy_steer(event, agent, "key")
    elif route == "tool-redirect":
        agent._executing_tools = True
        assert runner._redirect_active_turn(agent, event.text, "key", event)
    else:
        event.text = "/steer " + event.text
        assert await runner._busy_steer_command(event, "key", source)
    assert (opener.reply_expected, ctx.reply_expected, agent._turn_reply_expected) == (False, False, False)
    # A drain without a tool insertion must requeue the obligation with its text.
    apply_pending_steer_to_tool_results(agent, [{"role": "assistant", "content": "done"}], 1)
    assert (opener.reply_expected, ctx.reply_expected, agent._turn_reply_expected) == (False, False, False)
    assert "Please answer" in _incorporate(agent)
    assert (opener.reply_expected, ctx.reply_expected, agent._turn_reply_expected) == (True, True, True)


@pytest.mark.asyncio
async def test_coalesced_human_and_silent_relay_keep_human_obligation():
    runner, agent, opener, ctx, source = _running()
    human = MessageEvent(text="Please answer", source=source)
    relay = MessageEvent(text="[relay from=agent@example.com receipt=r2]\nFYI", source=source,
                         reply_expected=False)
    for event in (human, relay):
        outcome = await runner._resolve_busy_steer_or_redirect(event, "key", "steer", agent)
        assert outcome.steered
    assert opener.reply_expected is False
    delivered = _incorporate(agent)
    assert "Please answer" in delivered and "FYI" in delivered
    assert (opener.reply_expected, ctx.reply_expected, agent._turn_reply_expected) == (None, None, None)


def test_withdrawn_ticket_never_reports_delivery():
    from unittest.mock import Mock

    _, agent, _, _, _ = _running()
    delivered = Mock()
    assert agent.steer("queued", ticket="admission", on_delivered=delivered)
    assert agent.withdraw_steer("admission")
    messages = [{"role": "tool", "content": "done", "tool_call_id": "t1"}]
    apply_pending_steer_to_tool_results(agent, messages, 1)
    assert len(messages) == 1
    delivered.assert_not_called()
