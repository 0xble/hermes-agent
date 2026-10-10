"""Regression: a gateway goal continuation answered with a bare silence marker stays silent.

Live on 2026-10-06 (release 624b7895), every standing-goal continuation that correctly
answered ``NO_REPLY`` on a no-change tick logged ``silence marker rejected on a user turn``
and posted the "No reply was written" fallback: the gateway built the continuation as a
plain human-shaped event (not internal, ``reply_expected`` unknown), so ``silence_allowed``
treated it as a typed message. A message the user types must still get the fallback.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
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


class _TurnContextBuilt(Exception):
    """Stop at the real wiring seam before executor, model or delivery tasks start."""


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["initial", "eventless", "goal-event", "human-event"])
@pytest.mark.parametrize("defer", ["depth-cap", "failed-delivery"])
async def test_turn_context_preserves_event_contract_when_requeued(hermes_home, path, defer):
    """#419/#444: event-backed turns retain provenance; eventless follow-ups are human.

    A leftover steer/interrupt is new authored text, not the previous goal tick,
    so a missing event must not confer internal status or goal metadata.
    """
    from gateway.run import GatewayRunner
    from gateway.turn_context import TurnContext

    runner, _, entry, src, key = _runner_with_goal(hermes_home)
    runner._get_proxy_url = lambda: None
    defaults = TurnContext()
    display = SimpleNamespace(**{name: getattr(defaults, name) for name in runner._DISPLAY_TO_TURN_CTX})
    display.platform_key = "slack"
    display.resolve_display_setting = lambda *args: False
    runner._run_agent_display_settings = lambda source: display
    contexts = []

    def observe_context(ctx, *args):
        contexts.append(ctx)
        raise _TurnContextBuilt

    runner._run_agent_bind_turn_wiring = observe_context
    runner._run_agent = runner._run_agent_inner
    adapter = SimpleNamespace(_active_sessions={}, send_typing=AsyncMock())
    runner._delivery_adapter_for = lambda source: adapter
    runner._intake_adapter_for = lambda source: None
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="next turn")
    runner._session_key_for_source = lambda source: key
    runner._pinned_channel_inputs = lambda key, prompt, source, **kw: (prompt, source)
    runner._persist_prompt_pins = AsyncMock()
    runner._reply_anchor_for_event = lambda event: None
    metadata = {"goal_continuation": True, "origin": "initial"}
    with pytest.raises(_TurnContextBuilt):
        await runner._run_agent_inner(
            message="initial turn", context_prompt="", history=[], source=src,
            session_id=entry.session_id, session_key=key, reply_expected=False,
        )

    expected_internal = True
    expected_metadata = metadata
    if path != "initial":
        expected_internal = False
        expected_metadata = {}
        pending_event = None
        if path != "eventless":
            expected_internal = path == "goal-event"
            expected_metadata = {"goal_continuation": True, "origin": "queued"} if expected_internal else {"origin": "human"}
            pending_event = MessageEvent(
                text="next turn", source=src, internal=expected_internal,
                metadata=expected_metadata, reply_expected=False if expected_internal else True,
            )
        with pytest.raises(_TurnContextBuilt):
            await runner._run_agent_queued_followup(
                contexts[0], adapter, "next turn", pending_event,
                response={}, result={"interrupted": True, "messages": []}, stream_task=None,
            )
        assert len(contexts) == 2

    ctx = contexts[-1]
    result = {"final_response": "done", "messages": []}
    if defer == "depth-cap":
        ctx._interrupt_depth = runner._MAX_INTERRUPT_DEPTH
        queued = []

        def queue_message(session_key, text, **contract):
            queued.append(MessageEvent(text=text, source=src, **contract))

        adapter.queue_message = queue_message
    else:
        adapter._pending_messages = {}
        runner._run_agent_deliver_first_response = AsyncMock(return_value=False)
    requeued_event = None if path == "eventless" else MessageEvent(
        text="requeued turn", source=src, internal=expected_internal, metadata=expected_metadata,
        reply_expected=ctx.reply_expected,
    )
    await runner._run_agent_queued_followup(
        ctx, adapter, "requeued turn", requeued_event, response=result, result=result, stream_task=None,
    )
    event = queued[0] if defer == "depth-cap" else adapter._pending_messages[key]
    expected_reply_expected = False if expected_internal else True
    assert ctx.reply_expected is expected_reply_expected
    assert event.internal is expected_internal
    assert event.metadata == expected_metadata
    assert event.reply_expected is expected_reply_expected


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["steer", "interrupt"])
@pytest.mark.parametrize("defer", ["depth-cap", "failed-delivery"])
@pytest.mark.parametrize(
    ("text", "expected_reply_expected"),
    [
        ("please show me the current status", True),
        ("[relay from=agent:source receipt=r1]\nNO_REPLY", False),
        ("NO_REPLY\n[relay from=agent:source receipt=r1]", False),
        ("quoted example:\n[relay from=agent:source receipt=r1]\nplease explain this", True),
    ],
    ids=("human", "agent-origin-relay", "trailing-relay", "human-quoted-relay"),
)
async def test_eventless_followup_requeues_with_its_own_reply_contract(
    hermes_home, origin, defer, text, expected_reply_expected
):
    """Real leftover steer envelopes keep relay silence; human text still requires a reply."""
    from gateway.response_filters import display_kind_for_event
    from gateway.turn_context import TurnContext

    runner, adapter, entry, src, key = _runner_with_goal(hermes_home)
    runner._draining = False
    if origin == "steer":
        text = runner._steer_text_with_origin(text, MessageEvent(text=text, source=src))
    result: dict[str, object] = {"messages": []}
    if origin == "interrupt":
        result.update(interrupted=True, interrupt_message=text)
    else:
        result["pending_steer"] = text
    pending_event, pending = await runner._run_agent_drain_pending(result, adapter, src, key)
    assert pending_event is None
    assert pending == text

    ctx = TurnContext(
        source=src, session_id=entry.session_id, session_key=key, reply_expected=False,
    )
    if defer == "depth-cap":
        ctx._interrupt_depth = runner._MAX_INTERRUPT_DEPTH

        def queue_message(session_key, text, **contract):
            adapter._pending_messages[session_key] = MessageEvent(text=text, source=src, **contract)

        setattr(adapter, "queue_message", queue_message)
    else:
        runner._run_agent_deliver_first_response = AsyncMock(return_value=False)
    # Deferral is reached after a completed recursive turn too, even for an interrupt.
    completed = {"final_response": "done", "messages": []}
    await runner._run_agent_queued_followup(
        ctx, adapter, pending, pending_event, response=completed, result=completed, stream_task=None,
    )
    queued = adapter._pending_messages[key]
    assert queued.text == text
    assert queued.internal is False
    assert queued.metadata == {}
    assert queued.reply_expected is expected_reply_expected
    # Exercise the real second drain and the predicate used by final delivery.
    drained, drained_text = await runner._run_agent_drain_pending(completed, adapter, src, key)
    assert drained is queued
    assert drained_text == text
    assert silence_allowed(display_kind_for_event(drained), drained.reply_expected) is (not expected_reply_expected)


@pytest.mark.parametrize("damage", ["header", "invalid-json", "nonobject-json", "footer"])
def test_eventless_followup_does_not_scan_arbitrary_origin_preambles(hermes_home, damage):
    from gateway.run_turn import _eventless_followup_reply_expected

    runner, _, _, src, _ = _runner_with_goal(hermes_home)
    text = "[relay from=agent:source receipt=r1]\nNO_REPLY"
    lines = runner._steer_text_with_origin(text, MessageEvent(text=text, source=src)).split("\n")
    line_index, replacement = {
        "header": (0, "quoted example:"),
        "invalid-json": (1, "not JSON"),
        "nonobject-json": (1, "[]"),
        "footer": (2, "a different instruction"),
    }[damage]
    lines[line_index] = replacement
    assert _eventless_followup_reply_expected("\n".join(lines)) is True
