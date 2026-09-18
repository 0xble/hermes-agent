"""A queued one-turn reasoning override reaches the follow-up turn it was requested for.

``/reasoning high <prompt>`` stamps ``turn_reasoning_config`` on its own event. When that
event arrives while a turn is active it is queued, and the busy path preserves the stamp on
the queued event. The queued follow-up runs through the recursive ``_run_agent`` in
``_run_agent_queued_followup``, which must forward the SUCCESSOR event's config: without it the
prompt ran under the session/default reasoning configuration. The reverse must hold too: a
queued event that carries no override runs without one, even when the turn it was queued
behind had one (the override is per-event, never inherited across the chain).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform

SESSION_KEY = "agent:main:telegram:dm:5230977008"
HIGH = {"enabled": True, "effort": "high"}


def _source():
    return SimpleNamespace(platform=Platform.TELEGRAM, chat_id="5230977008", thread_id=None,
                           chat_type="dm")


def _runner_and_ctx(*, preceding_override=None):
    from gateway.run import GatewayRunner

    runner = MagicMock()
    runner._MAX_INTERRUPT_DEPTH = 8
    runner._run_agent = AsyncMock(return_value={"final_response": "done", "messages": []})
    runner._run_agent_deliver_first_response = AsyncMock(return_value=True)
    runner._is_goal_continuation_event = MagicMock(return_value=False)
    runner._session_key_for_source = MagicMock(return_value=SESSION_KEY)
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="the follow-up")
    runner._reply_anchor_for_event = MagicMock(return_value="6002")
    runner._adapter_for_source = MagicMock(return_value=None)
    runner._refresh_agent_cache_message_count = AsyncMock()
    source = _source()
    turn_ctx = SimpleNamespace(
        source=source, session_id="sid", session_key=SESSION_KEY, run_generation=1,
        _interrupt_depth=0, history=[], _status_thread_metadata=None,
        context_prompt=None, result_holder=[None],
        # The preceding turn's own override, as the turn context carries it.
        turn_reasoning_config=preceding_override,
    )
    return GatewayRunner, runner, turn_ctx, source


async def _run_followup(GatewayRunner, runner, turn_ctx, pending_event):
    await GatewayRunner._run_agent_queued_followup(
        runner, turn_ctx, adapter=None, pending="hi again", pending_event=pending_event,
        response="resp", result={"interrupted": False, "messages": []}, stream_task=None)
    runner._run_agent.assert_awaited_once()
    return runner._run_agent.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_metadata", [None, {}])
async def test_queued_followup_forwards_its_own_reasoning_override(stored_metadata):
    """A queued ``/reasoning high`` event runs its prompt under the requested override."""
    from gateway.platforms.event import MessageEvent
    from gateway.restart_inbox import serialize_event, deserialize_event
    from gateway.session import SessionSource

    GatewayRunner, runner, turn_ctx, source = _runner_and_ctx(preceding_override=None)
    pending_event = MessageEvent(
        source=SessionSource(platform=source.platform, chat_id=source.chat_id, chat_type="dm"),
        message_id="6002", text="explain this", turn_reasoning_config=dict(HIGH))
    assert pending_event.metadata == {}
    payload = json.loads(serialize_event(pending_event))
    payload["metadata"] = stored_metadata
    pending_event = deserialize_event(json.dumps(payload))
    assert pending_event.metadata == {}  # serialized null is normalized at rehydration

    kwargs = await _run_followup(GatewayRunner, runner, turn_ctx, pending_event)

    assert kwargs.get("turn_reasoning_config") == HIGH, \
        "the queued event's one-turn reasoning override did not reach the follow-up turn"
    assert kwargs["persist_user_display_metadata"] == {"gateway_input_owner": None}


@pytest.mark.asyncio
async def test_queued_followup_does_not_inherit_preceding_turns_override():
    """A queued event without an override runs plain even when the turn before it had one."""
    GatewayRunner, runner, turn_ctx, source = _runner_and_ctx(preceding_override=dict(HIGH))
    pending_event = SimpleNamespace(
        source=source, message_id="6002", channel_prompt=None, message_type=None,
        # ``internal``/``metadata``: the terminal turn reads them to pick the notification
        # category it records the outer final send under.
        text="and now this", turn_reasoning_config=None, internal=False, metadata=None)

    kwargs = await _run_followup(GatewayRunner, runner, turn_ctx, pending_event)

    assert kwargs.get("turn_reasoning_config") is None, \
        "the follow-up inherited the preceding turn's one-turn reasoning override"


@pytest.mark.asyncio
async def test_steer_text_followup_without_an_event_runs_without_an_override():
    """A leftover-steer follow-up (text only, no event) has no successor event to draw from."""
    GatewayRunner, runner, turn_ctx, _source_ = _runner_and_ctx(preceding_override=dict(HIGH))

    kwargs = await _run_followup(GatewayRunner, runner, turn_ctx, pending_event=None)

    assert kwargs.get("turn_reasoning_config") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, {}, {"gateway_input_owner": {"token": "owned-input"}}])
async def test_live_queued_event_metadata_preserves_owner_after_consumption(metadata):
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from tests.gateway.restart_test_helpers import make_restart_runner

    GatewayRunner, followup, turn_ctx, source = _runner_and_ctx()
    queue_runner, adapter = make_restart_runner()
    queue_runner._adapter_for_source = lambda _: adapter
    queue_runner._pending_event_audio_paths = lambda _: []
    event = MessageEvent(
        source=SessionSource(platform=source.platform, chat_id=source.chat_id, chat_type="dm"),
        message_id="6002", text="live queued input", metadata=metadata,
        turn_reasoning_config=dict(HIGH),
    )
    queue_runner._queue_or_replace_pending_event(SESSION_KEY, event)
    consumed, text = await queue_runner._run_agent_drain_pending(
        {"messages": []}, adapter, event.source, SESSION_KEY)
    assert consumed is event
    assert text == event.text
    assert SESSION_KEY not in adapter._pending_messages
    kwargs = await _run_followup(GatewayRunner, followup, turn_ctx, consumed)
    assert kwargs["persist_user_display_metadata"] == {
        "gateway_input_owner": (metadata or {}).get("gateway_input_owner")}
    assert kwargs["persist_user_display_kind"] is None
    assert kwargs["turn_reasoning_config"] == HIGH
    assert event.metadata is metadata  # no ownership or event rewrite
