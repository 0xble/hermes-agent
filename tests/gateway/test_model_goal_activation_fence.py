"""A mid-turn /goal clear or pause fences stale model activation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["clear", "pause", "stop", "done"])
async def test_busy_goal_control_fences_the_running_turn(command):
    from gateway.run import GatewayRunner
    from hermes_cli.goals import model_goal_activation_blocked

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running_agents = {
        "chat": SimpleNamespace(session_id="session-1", _current_turn_id="turn-1")
    }
    runner._handle_goal_command = AsyncMock(return_value="controlled")
    event = SimpleNamespace(get_command_args=lambda: command)

    result = await GatewayRunner._busy_goal_command(runner, event, "chat", None)

    assert result == "controlled"
    assert model_goal_activation_blocked("session-1", "turn-1") is True
    runner._handle_goal_command.assert_awaited_once_with(event)
