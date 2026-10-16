"""A typed message must not wait behind a turn nobody typed.

With ``busy_input_mode: queue`` a text follow-up waits for the running turn to finish. That is
right for the user's own turns, but goal continuations, boot auto-resume, completion follow-ups
and agent relays also run as turns in the same topic, and some run for many minutes. On
2026-10-11 typed messages (including a "pause scorecard work" request) sat 57-85 minutes behind
such turns. A typed message now redirects an autonomous turn at its next safe boundary instead
(an in-flight model request is re-issued, a foreground command yields to the background, nothing
is killed); the user's own turns keep queue semantics.
"""

from __future__ import annotations

import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.platforms.base import Platform, SessionSource, build_session_key  # noqa: E402
from gateway.platforms.event import (  # noqa: E402
    GOAL_CONTINUATION_METADATA_KEY,
    MessageEvent,
    MessageType,
)
from gateway.run import GatewayRunner  # noqa: E402

_SOURCE = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="user1",
                        thread_id="311556")
_RELAY = "[relay from=hermes:default/cron_8a0371c2d490_20261011_123143 receipt=fee1f2f3]"


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner._busy_input_mode = "queue"
    runner._busy_text_mode = "queue"
    runner.adapters = {}
    runner.config = MagicMock()
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = Platform.TELEGRAM
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner


def _agent(*, redirect_ok: bool = True) -> MagicMock:
    agent = MagicMock()
    agent._supports_active_turn_redirect = True
    agent._turn_user_intervened = False
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent.redirect.return_value = redirect_ok
    agent.steer.return_value = True
    agent.get_activity_summary.return_value = {"api_call_count": 3, "max_iterations": 60}
    return agent


def _start_turn(runner: GatewayRunner, opener: MessageEvent, agent: MagicMock) -> str:
    key = build_session_key(_SOURCE)
    runner._running_agents[key] = agent
    runner._session_state(key).turn.event = opener
    return key


def _typed(text: str = "Do this. Pause scorecard work") -> MessageEvent:
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_SOURCE, message_id="m-human")


def _goal_tick() -> MessageEvent:
    return MessageEvent(text="[Continuing toward your standing goal] Goal: ...", source=_SOURCE,
                        reply_expected=False, metadata={GOAL_CONTINUATION_METADATA_KEY: True})


def _boot_resume() -> MessageEvent:
    return MessageEvent(text="", source=_SOURCE, internal=True)


def _relay(text: str = "📈 Build webinar scorecard") -> MessageEvent:
    return MessageEvent(text=f"{text}\n{_RELAY}", source=_SOURCE, message_id="m-relay")


def _fyi_relay() -> MessageEvent:
    return MessageEvent(
        text=(
            "📨 Relay title\nBrief\n\nDetails\n"
            "From: Source Agent · FYI, no action requested\n"
            "[relay from=hermes:default/source receipt=fyi-1]"
        ),
        source=_SOURCE,
        message_id="m-fyi",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("opener", [_goal_tick, _boot_resume, _relay], ids=["goal", "resume", "relay"])
async def test_typed_message_redirects_autonomous_turn(opener) -> None:
    runner = _runner()
    agent = _agent()
    key = _start_turn(runner, opener(), agent)

    handled = await runner._handle_active_session_busy_message(_typed(), key)

    assert handled is True
    agent.redirect.assert_called_once()
    assert "Pause scorecard work" in agent.redirect.call_args.args[0]
    agent.interrupt.assert_not_called()  # a safe boundary: never kill the running tool
    assert key not in runner.adapters[Platform.TELEGRAM]._pending_messages


@pytest.mark.asyncio
async def test_redirect_miss_steers_instead_of_queueing() -> None:
    """Between a model response and its tool batch redirect() declines; the text still lands."""
    runner = _runner()
    agent = _agent(redirect_ok=False)
    key = _start_turn(runner, _goal_tick(), agent)

    assert await runner._handle_active_session_busy_message(_typed(), key) is True

    agent.steer.assert_called_once()
    agent.interrupt.assert_not_called()
    assert key not in runner.adapters[Platform.TELEGRAM]._pending_messages


@pytest.mark.asyncio
async def test_users_own_turn_keeps_queue_semantics() -> None:
    runner = _runner()
    agent = _agent()
    key = _start_turn(runner, _typed("Study every skill from these repos"), agent)

    handled = await runner._handle_active_session_busy_message(_typed("and also this"), key)

    assert handled is False  # the base adapter queues it behind the user's own turn
    agent.redirect.assert_not_called()
    agent.steer.assert_not_called()
    agent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_turn_the_user_already_joined_keeps_queue_semantics() -> None:
    runner = _runner()
    agent = _agent()
    agent._turn_user_intervened = True
    key = _start_turn(runner, _goal_tick(), agent)

    assert await runner._handle_active_session_busy_message(_typed(), key) is False
    agent.redirect.assert_not_called()


@pytest.mark.asyncio
async def test_relay_does_not_preempt_autonomous_turn() -> None:
    runner = _runner()
    agent = _agent()
    key = _start_turn(runner, _goal_tick(), agent)

    assert await runner._handle_active_session_busy_message(_relay("🔄 LPG weekly review failed"), key) is False
    agent.redirect.assert_not_called()
    agent.steer.assert_not_called()


@pytest.mark.asyncio
async def test_fyi_relay_queues_without_interrupting_in_interrupt_mode() -> None:
    runner = _runner()
    runner._busy_input_mode = runner._busy_text_mode = "interrupt"
    runner._relay_fyi_passive_enabled = lambda source=None: True
    agent = _agent()
    key = _start_turn(runner, _goal_tick(), agent)
    fyi = _fyi_relay()

    assert await runner._handle_active_session_busy_message(fyi, key) is True
    agent.interrupt.assert_not_called()
    agent.redirect.assert_not_called()
    agent.steer.assert_not_called()
    assert runner.adapters[Platform.TELEGRAM]._pending_messages[key] is fyi
