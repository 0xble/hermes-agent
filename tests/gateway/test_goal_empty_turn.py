"""Gateway goal admission follows the turn outcome, not visible reply length."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run_goals import GatewayGoalsMixin


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   ", "[SILENT]", "failure notice"])
@pytest.mark.parametrize("outcome", ["success", "failed", "interrupted", "incomplete", "legacy"])
async def test_post_turn_goal_admission_uses_outcome(text, outcome):
    runner = GatewayGoalsMixin()
    runner.async_session_store = SimpleNamespace(get_or_create_session=AsyncMock(
        return_value=SimpleNamespace(session_id="gateway-empty-turn")))
    runner._post_turn_goal_continuation = AsyncMock()
    runner._post_turn_loop_completion = AsyncMock()
    runner._is_user_turn_event = lambda event: True
    payload = {"final_response": text}
    event = SimpleNamespace(metadata={})
    fields, succeeded = {
        "success": ({"completed": True}, True),
        "failed": ({"failed": True}, False),
        "interrupted": ({"interrupted": True}, False),
        "incomplete": ({"completed": False}, None),
        "legacy": ({}, None),
    }[outcome]
    payload.update(fields)
    if succeeded is not None:
        event._agent_turn_succeeded = succeeded
    await runner._run_post_turn_hooks(agent_result=payload, source=None, is_internal=False, event=event)
    admitted = outcome == "success" or (outcome == "legacy" and bool(text.strip()))
    assert runner._post_turn_goal_continuation.await_count == int(admitted)
    runner._post_turn_loop_completion.assert_awaited_once()
    if admitted:
        assert runner._post_turn_goal_continuation.await_args.kwargs["final_response"] == text
