"""A typed message steered into a running relay turn must still get a visible answer.

Regression for the 2026-10-11 topic incident: a relay ("FYI, no reply needed") opened a turn with
``reply_expected=False``; the user's typed question preempted it and was folded in through the busy
steer path; the model followed the relay framing and ended on a bare ``NO_REPLY``. The gateway then
replaced the marker with the "no reply was written" warning instead of an answer.

Drives the real pieces end to end: ``AIAgent.run_conversation`` with a mocked model, the gateway's
``_steer_running_agent`` + ``_fold_into_running_turn`` while the model call is in flight, and
``_hmwa_shape_agent_response`` for the delivered text. The fold happens after turn start, so this
also proves the re-prompt reads ``_turn_reply_expected`` at final-response time.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import gateway.run as gateway_run
from agent.conversation_loop import _GATEWAY_SILENCE_REPROMPT_NUDGE
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.response_filters import is_intentional_silence_response
from gateway.run_turn import _unexpected_silence_reply
from gateway.session import SessionSource
from gateway.session_state import TurnState
from tests.agent.test_run_agent import _mock_response, _mock_tool_call

SESSION_KEY = "agent:main:telegram:group:-1001:212659"
RELAY_TEXT = "[relay from=session/cron receipt=r-1]\nAI & Agents Brief. FYI, no reply needed."
TYPED_TEXT = "what did the brief say about agents?"


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", user_id="12345")


@pytest.fixture()
def agent():
    from run_agent import AIAgent
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._gateway_session_key = SESSION_KEY
    return agent


def _run_relay_turn(agent, finals, *, typed_steer):
    """Run a relay-opened turn; optionally fold a typed message in during the first model call."""
    runner = gateway_run.GatewayRunner(GatewayConfig())
    relay = MessageEvent(text=RELAY_TEXT, source=_source(), message_id="relay-1", reply_expected=False)
    state = runner._session_state(SESSION_KEY)
    state.turn = TurnState(agent=agent, event=relay, ctx=SimpleNamespace(reply_expected=False))

    def first_call(*_args, **_kwargs):
        if typed_steer:
            typed = MessageEvent(text=TYPED_TEXT, source=_source(), message_id="typed-1")
            assert runner._human_preempts_autonomous_turn(typed, state)
            assert runner._steer_running_agent(agent, TYPED_TEXT)
            assert runner._fold_into_running_turn(agent, SESSION_KEY, typed) is state.turn
        return _mock_response(
            content="", finish_reason="tool_calls",
            tool_calls=[_mock_tool_call(name="web_search", arguments="{}", call_id="call_1")],
        )

    agent.client.chat.completions.create.side_effect = _sequence(
        [first_call] + [_mock_response(content=text, finish_reason="stop") for text in finals]
    )
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("model_tools.handle_function_call", return_value="ok"),
    ):
        result = agent.run_conversation(
            RELAY_TEXT, persist_user_display_metadata={"reply_expected": False},
        )
    return runner, relay, result


def _sequence(stages):
    stages = list(stages)

    def call(*args, **kwargs):
        stage = stages.pop(0)
        return stage(*args, **kwargs) if callable(stage) else stage
    return call


async def _delivered(runner, relay, result):
    """The text the gateway would deliver, judged against the turn event's reply expectation."""
    response, intentional, _ = await runner._hmwa_shape_agent_response(
        result, relay.source, history=[], session_entry=SimpleNamespace(session_id=result.get("session_id")),
        session_key=None, _quick_key=SESSION_KEY, run_generation=1,
        _run_start_session_id=result.get("session_id"), _platform_name="telegram", _msg_start_time=0.0,
        reply_expected=relay.reply_expected,
    )
    return response, intentional


def _user_rows_sent(agent, call_index):
    kwargs = agent.client.chat.completions.create.call_args_list[call_index].kwargs
    return [m["content"] for m in kwargs["messages"] if m.get("role") == "user"]


@pytest.mark.asyncio
async def test_typed_steer_into_relay_turn_reprompts_once_and_delivers_answer(agent):
    runner, relay, result = await _call(agent, ["NO_REPLY", "The brief covered agent evals."], True)

    calls = agent.client.chat.completions.create.call_args_list
    assert len(calls) == 3
    assert any(TYPED_TEXT in row for row in _user_rows_sent(agent, 1))
    assert _user_rows_sent(agent, 2)[-1] == _GATEWAY_SILENCE_REPROMPT_NUDGE
    assert relay.reply_expected is None and agent._turn_reply_expected is None
    response, intentional = await _delivered(runner, relay, result)
    assert response == "The brief covered agent evals."
    assert intentional is False
    assert response != _unexpected_silence_reply()


@pytest.mark.asyncio
async def test_second_silence_marker_after_reprompt_falls_back_once(agent):
    runner, relay, result = await _call(agent, ["NO_REPLY", "NO_REPLY"], True)

    assert agent.client.chat.completions.create.call_count == 3
    response, intentional = await _delivered(runner, relay, result)
    assert response == _unexpected_silence_reply()
    assert intentional is False


@pytest.mark.asyncio
async def test_relay_only_turn_stays_silent_without_reprompt(agent):
    runner, relay, result = await _call(agent, ["NO_REPLY"], False)

    assert agent.client.chat.completions.create.call_count == 2
    assert all(_GATEWAY_SILENCE_REPROMPT_NUDGE not in row for row in _user_rows_sent(agent, 1))
    response, intentional = await _delivered(runner, relay, result)
    assert intentional is True
    assert is_intentional_silence_response(response)


async def _call(agent, finals, typed_steer):
    import asyncio
    # run_conversation is synchronous and drives its own loop internals; keep it off this loop.
    return await asyncio.to_thread(_run_relay_turn, agent, finals, typed_steer=typed_steer)
