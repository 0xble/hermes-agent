"""Goal mutations invalidate only this session's synthetic work through the real gateway FIFO."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, build_session_key
from hermes_cli.goals import GoalContract, GoalManager
from tests.gateway.test_goal_continuation_drain import (
    _DrainProbeAdapter, _slack_thread_source, hermes_home,  # noqa: F401
)


pytestmark = pytest.mark.usefixtures("hermes_home")


def test_continuation_classifier_uses_event_provenance_with_raw_string_fallback():
    source = _slack_thread_source()
    text = "[Continuing toward your standing goal]\nGoal: Old objective"
    assert GatewayRunner._is_goal_continuation_event(text)
    assert not GatewayRunner._is_goal_continuation_event(MessageEvent(text=text, source=source))
    assert GatewayRunner._is_goal_continuation_event(
        MessageEvent(text="new formatter", source=source, metadata={"goal_continuation": True}),
    )
    assert not GatewayRunner._is_goal_continuation_event(None)
    from collections import defaultdict
    from hermes_cli.goals_prompt_registry import GOAL_PROMPT_REGISTRY

    for row in GOAL_PROMPT_REGISTRY:
        if row["formatter"] not in {"plain", "contract", "subgoals"}:
            continue
        rendered = row["template"].format_map(defaultdict(lambda: "Quoted release requirements"))
        # Persisted raw text uses legacy formatter recognition; a live human event never does.
        assert GatewayRunner._is_goal_continuation_event(rendered)
        for typed in (rendered, f"I quoted this prompt:\n{rendered}"):
            assert not GatewayRunner._is_goal_continuation_event(MessageEvent(text=typed, source=source))


def _runner():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True, token="x")})
    runner._queued_events = {}
    source = _slack_thread_source()
    key = build_session_key(source)
    entry = SessionEntry(
        session_key=key, session_id="goal-mutation", created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.SLACK, chat_type="channel",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._generate_session_key.return_value = key
    adapter = _DrainProbeAdapter()
    runner.adapters = {Platform.SLACK: adapter}
    return runner, adapter, source, key, entry


@pytest.mark.asyncio
@pytest.mark.parametrize("command,old,current", [
    ("replace New objective", "Old objective", "New objective"),
    ("remove constraint 1", "No downtime.", "No secrets."),
    ("remove check 1", "Security audit passes.", "Smoke test passes."),
    ("remove subgoal 1", "Migrate Willow", "Update release notes"),
])
@pytest.mark.parametrize("goal_first", [True, False])
async def test_busy_mutation_rebuilds_queued_goal_and_drains_current_prompt(
    command, old, current, goal_first,
):
    runner, adapter, source, key, entry = _runner()
    mgr = GoalManager(entry.session_id)
    mgr.set("Old objective", contract=GoalContract(
        constraints="No downtime.; No secrets.", verification="Security audit passes.; Smoke test passes.",
    ))
    mgr.add_subgoal("Migrate Willow")
    mgr.add_subgoal("Update release notes")
    stale = runner._synthetic_prompt_event(source, mgr.next_continuation_prompt(), goal_continuation=True)
    # Even a human quoting the exact continuation header must survive: provenance owns invalidation.
    human = MessageEvent(text=stale.text, source=source, message_id="typed-1", reply_expected=True)
    second_human = MessageEvent(text="Keep my queued message", source=source, message_id="typed-2")
    queue = [stale, human, stale, second_human] if goal_first else [human, stale, second_human, stale]
    for event in queue:
        runner._enqueue_fifo(key, event, adapter)
    other_key = key + ":other"
    other = runner._synthetic_prompt_event(source, stale.text, goal_continuation=True)
    runner._enqueue_fifo(other_key, other, adapter)
    command_event = MessageEvent(text="/goal " + command, source=source, message_id="command")

    response = await runner._busy_goal_command(command_event, key, source)

    assert "✓" in response
    assert adapter._pending_messages[other_key] is other
    events = []
    for _ in range(runner._queue_depth(key, adapter=adapter)):
        event, _ = await runner._run_agent_drain_pending({"messages": []}, adapter, source, key)
        events.append(event)
    assert events[:2] == [human, second_human]
    assert len(events) == 3
    continuation = events[-1]
    assert continuation is not stale
    assert continuation.text == GoalManager(entry.session_id).next_continuation_prompt()
    assert old not in continuation.text and current in continuation.text
    assert continuation.metadata["goal_continuation"] is True
    assert continuation.reply_expected is False

    # Exercise run_turn's actual queued follow-up consumer, stopping only at model execution.
    runner._run_agent = AsyncMock(return_value={"final_response": "next", "messages": []})
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
        side_effect=lambda **kwargs: kwargs["event"].text,
    )
    runner._pinned_channel_inputs = lambda key, prompt, source, **kwargs: (prompt, source)
    runner._persist_prompt_pins = AsyncMock()
    ctx = SimpleNamespace(
        source=source, session_id=entry.session_id, session_key=key, run_generation=1,
        _interrupt_depth=0, history=[], _status_thread_metadata={}, context_prompt=None,
        result_holder=[None], _post_delivery_adapter=adapter, channel_prompt=None,
    )
    await runner._run_agent_queued_followup(
        ctx, adapter, continuation.text, continuation, "partial",
        {"interrupted": True, "messages": []}, None,
    )
    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.await_args.kwargs["message"] == continuation.text
    assert old not in runner._run_agent.await_args.kwargs["message"]
    assert runner._run_agent.await_args.kwargs["reply_expected"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["replace", "remove check 99"])
async def test_rejected_mutation_preserves_existing_fifo(command):
    runner, adapter, source, key, entry = _runner()
    mgr = GoalManager(entry.session_id)
    mgr.set("Old objective", contract=GoalContract(verification="Security audit passes."))
    stale = runner._synthetic_prompt_event(source, mgr.next_continuation_prompt(), goal_continuation=True)
    human = MessageEvent(text="real message", source=source, message_id="typed")
    runner._enqueue_fifo(key, stale, adapter)
    runner._enqueue_fifo(key, human, adapter)
    before = mgr.state.to_json()

    response = await runner._busy_goal_command(MessageEvent(text="/goal " + command, source=source), key, source)

    assert "✓" not in response
    assert adapter._pending_messages[key] is stale
    assert runner._overflow_queue(key) == [human]
    assert GoalManager(entry.session_id).state.to_json() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["replace New objective", "remove check 1"])
async def test_paused_mutation_clears_stale_goal_without_resuming_or_dropping_user(command):
    runner, adapter, source, key, entry = _runner()
    mgr = GoalManager(entry.session_id)
    mgr.set("Old objective", contract=GoalContract(verification="Security audit passes."), max_turns=1)
    stale = runner._synthetic_prompt_event(source, mgr.next_continuation_prompt(), goal_continuation=True)
    mgr.state.turns_used = 1
    mgr._save()   # the evaluator commits spent turns; pause() only edits lifecycle on the live row
    mgr.pause(reason="turn budget exhausted (1/1)")
    human = MessageEvent(text="my real message", source=source, message_id="typed")
    runner._enqueue_fifo(key, stale, adapter)
    runner._enqueue_fifo(key, human, adapter)

    response = await runner._busy_goal_command(MessageEvent(text="/goal " + command, source=source), key, source)

    assert "✓" in response
    assert adapter._pending_messages[key] is human
    assert runner._queue_depth(key, adapter=adapter) == 1
    current = GoalManager(entry.session_id)
    assert current.state.status == "paused" and current.state.turns_used == 1
    assert current.next_continuation_prompt() is None
