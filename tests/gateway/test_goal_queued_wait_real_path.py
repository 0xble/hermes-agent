"""A queued chain's terminal input, not its head, owns the goal wait clock."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.platforms.base import MessageEvent, MessageType
from gateway.turn_context import TurnContext
from hermes_cli import goals
from tests.gateway.test_autonomous_wake_pacing import _goal_runner, hermes_home


def _prepare_handler(runner, entry, source):
    """Keep the real handler/carrier and post-turn judge; stub presentation and model I/O."""
    runner._hmwa_resolve_session = AsyncMock(return_value=(source, entry, entry.session_key))
    runner._hmwa_prepare_turn = AsyncMock(return_value=(
        runner._PreparedTurn([], "", "head", None, None, None), None))
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._persist_prompt_pins = AsyncMock()
    runner._pinned_channel_inputs = lambda key, prompt, src, **kw: (prompt, src)
    runner._reply_anchor_for_event = lambda event: None
    runner._hmwa_stop_typing_for_turn = AsyncMock()
    runner._is_session_run_current = lambda *args: True
    runner._hmwa_shape_agent_response = AsyncMock(return_value=("progress", False, []))
    runner._hmwa_prepend_reasoning = lambda result, response, *args: response
    runner._hmwa_runtime_footer_line = lambda *args: None
    runner._hmwa_post_turn_hooks = AsyncMock()
    runner._hmwa_classify_turn_failure = lambda *args: (False, False, False)
    runner._hmwa_compression_exhaustion_reset = AsyncMock(return_value=("progress", entry))
    runner._hmwa_persist_turn_transcript = AsyncMock()
    runner._hmwa_deliver_turn_response = AsyncMock(return_value="progress")
    runner._clear_session_env = lambda tokens: None
    runner._MAX_INTERRUPT_DEPTH = 8
    runner._run_agent_deliver_first_response = AsyncMock(return_value=True)
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(side_effect=lambda **kw: kw["event"].text)
    runner._intake_adapter_for = lambda source: None
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    runner._post_turn_loop_completion = AsyncMock()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "process_registry_synthetic", "delegation_result"])
@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("rearm_in_terminal", [False, True])
async def test_queued_terminal_turn_owns_wait_age(
    hermes_home, monkeypatch, origin, depth, rearm_in_terminal,
):
    runner, adapter, entry, source = _goal_runner()
    _prepare_handler(runner, entry, source)
    clock = [50_000.0]
    monkeypatch.setattr(goals.time, "time", lambda: clock[0])
    manager = goals.GoalManager(entry.session_id)
    manager.set("finish the report when evidence arrives")
    head = MessageEvent(text="head", message_type=MessageType.TEXT, source=source)
    # Same source as the inbound head slot claim. It must not remain the terminal turn's clock.
    runner._session_state(entry.session_key).conversation.last_turn_started_at = clock[0]
    results = []
    starts = []

    async def model_and_drain(**kwargs):
        turn = kwargs.get("_interrupt_depth", 0)
        starts.append(clock[0])
        result = {"completed": True, "final_response": "progress", "messages": []}
        if turn == 0 or (turn == depth and rearm_in_terminal):
            clock[0] += 1
            manager.wait_for_seconds(86_400, reason="awaiting the report")
        if turn < depth:
            clock[0] += 10
            terminal = turn + 1 == depth
            event = MessageEvent(
                text="report arrived" if terminal else "intermediate input",
                message_type=MessageType.TEXT, source=source,
                internal=terminal and origin is not None,
                metadata={"notification_origin": origin} if terminal and origin else {},
            )
            ctx = TurnContext(
                source=source, session_id=entry.session_id, session_key=entry.session_key,
                run_generation=1, history=[], _interrupt_depth=turn,
            )
            result = await runner._run_agent_queued_followup(
                ctx, adapter, event.text, event, "progress", result, None)
        results.append(result)
        return result

    runner._run_agent = model_and_drain
    with patch.object(goals, "judge_goal", return_value=("continue", "more work", False, None, False)) as judge:
        assert await runner._handle_message_with_agent(head, source, entry.session_key, 1) == "progress"
        assert len(starts) == depth + 1
        assert head._post_turn_goal_identity["user_turn"] is (origin is None)
        if origin:
            assert head.metadata["notification_origin"] == origin
        # The real handler above transfers the terminal identity/origin from the actual drain.
        await runner._run_post_turn_hooks(
            agent_result=results[-1], source=source, is_internal=False, event=head)

    persisted = goals.load_goal(entry.session_id)
    assert persisted is not None
    if rearm_in_terminal:
        judge.assert_not_called()
        assert persisted.waiting_since == starts[-1] + 1
        assert persisted.waiting_until > clock[0]
    else:
        judge.assert_called_once()
        assert persisted.waiting_since == 0
        assert persisted.waiting_until == 0
