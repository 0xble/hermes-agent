"""Regression: a gateway goal continuation answered with a bare silence marker stays silent.

Live on 2026-10-06 (release 624b7895), every standing-goal continuation that correctly
answered ``NO_REPLY`` on a no-change tick logged ``silence marker rejected on a user turn``
and posted the "No reply was written" fallback: the gateway built the continuation as a
plain human-shaped event (not internal, ``reply_expected`` unknown), so ``silence_allowed``
treated it as a typed message. A message the user types must still get the fallback.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
import uuid

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.response_filters import silence_allowed
from gateway.session import SessionEntry, build_session_key

from tests.gateway.test_goal_continuation_drain import (  # noqa: F401  (fixture)
    _DrainProbeAdapter,
    _slack_thread_source,
    hermes_home,
)


def _runner_with_goal(hermes_home):
    from gateway.run import GatewayRunner
    from hermes_cli.goals import GoalManager

    src = _slack_thread_source()
    key = build_session_key(src)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True, token="x")})
    runner._queued_events = {}
    entry = SessionEntry(
        session_key=key, session_id=f"goal-silence-{uuid.uuid4().hex[:8]}", created_at=datetime.now(),
        updated_at=datetime.now(), platform=Platform.SLACK, chat_type="channel",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._generate_session_key.return_value = key
    adapter = _DrainProbeAdapter()
    runner.adapters = {Platform.SLACK: adapter}
    GoalManager(entry.session_id).set("ship it")
    return runner, adapter, entry, src, key


@pytest.mark.asyncio
async def test_post_turn_goal_continuation_is_not_a_typed_turn(hermes_home):
    runner, adapter, entry, src, key = _runner_with_goal(hermes_home)
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "still needs work", False, None, False)):
        await runner._post_turn_goal_continuation(session_entry=entry, source=src, final_response="partial")

    queued = adapter._pending_messages[key]
    assert queued.text.startswith("[Continuing toward your standing goal]")
    assert queued.reply_expected is False
    # The exact verdict run_turn applies to a bare NO_REPLY on this turn.
    assert silence_allowed(None, queued.reply_expected) is True


def test_typed_message_still_gets_the_fallback():
    typed = MessageEvent(text="please do X", message_type=MessageType.TEXT, source=_slack_thread_source())
    assert typed.reply_expected is None
    assert silence_allowed(None, typed.reply_expected) is False


def test_typed_message_absorbed_into_a_continuation_restores_the_human_contract():
    """A message Brian types that is folded into a queued continuation must not inherit silence."""
    from gateway.run import GatewayRunner

    continuation = GatewayRunner._synthetic_prompt_event(
        _slack_thread_source(), "[Continuing toward your standing goal]\nGoal: ship it", reply_expected=False,
    )
    typed = MessageEvent(text="status?", message_type=MessageType.TEXT, source=_slack_thread_source())
    continuation.absorb_reply_expected(typed)
    assert silence_allowed(None, continuation.reply_expected) is False


def test_heartbeat_and_loop_prompts_keep_their_existing_contract():
    """Only goal continuations opt in; other synthetic prompts are unchanged by default."""
    from gateway.run import GatewayRunner

    event = GatewayRunner._synthetic_prompt_event(_slack_thread_source(), "loop tick")
    assert event.reply_expected is None
