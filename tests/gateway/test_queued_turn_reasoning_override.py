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
async def test_queued_followup_forwards_its_own_reasoning_override():
    """A queued ``/reasoning high`` event runs its prompt under the requested override."""
    GatewayRunner, runner, turn_ctx, source = _runner_and_ctx(preceding_override=None)
    pending_event = SimpleNamespace(
        source=source, message_id="6002", channel_prompt=None, message_type=None,
        text="explain this", turn_reasoning_config=dict(HIGH))

    kwargs = await _run_followup(GatewayRunner, runner, turn_ctx, pending_event)

    assert kwargs.get("turn_reasoning_config") == HIGH, \
        "the queued event's one-turn reasoning override did not reach the follow-up turn"


@pytest.mark.asyncio
async def test_queued_followup_does_not_inherit_preceding_turns_override():
    """A queued event without an override runs plain even when the turn before it had one."""
    GatewayRunner, runner, turn_ctx, source = _runner_and_ctx(preceding_override=dict(HIGH))
    pending_event = SimpleNamespace(
        source=source, message_id="6002", channel_prompt=None, message_type=None,
        text="and now this", turn_reasoning_config=None)

    kwargs = await _run_followup(GatewayRunner, runner, turn_ctx, pending_event)

    assert kwargs.get("turn_reasoning_config") is None, \
        "the follow-up inherited the preceding turn's one-turn reasoning override"


@pytest.mark.asyncio
async def test_steer_text_followup_without_an_event_runs_without_an_override():
    """A leftover-steer follow-up (text only, no event) has no successor event to draw from."""
    GatewayRunner, runner, turn_ctx, _source_ = _runner_and_ctx(preceding_override=dict(HIGH))

    kwargs = await _run_followup(GatewayRunner, runner, turn_ctx, pending_event=None)

    assert kwargs.get("turn_reasoning_config") is None
