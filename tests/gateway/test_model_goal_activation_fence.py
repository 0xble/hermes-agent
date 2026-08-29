"""A mid-turn /goal clear or pause advances the control revision."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["clear", "pause", "stop", "done"])
async def test_busy_goal_control_advances_the_persisted_revision(
    command,
    tmp_path,
    monkeypatch,
):
    from gateway.run import GatewayRunner
    from hermes_cli import goals
    from hermes_cli.goals import get_goal_control_revision

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()

    runner = object.__new__(GatewayRunner)
    event = SimpleNamespace(
        text=f"/goal {command}",
        platform="telegram",
        user_id="user-1",
        metadata={"chat_id": "chat-1"},
        get_command_args=lambda: command,
    )
    agent = SimpleNamespace(
        session_id="session-1",
        _current_turn_id="turn-1",
    )
    runner._running_agents = {"telegram:chat-1": agent}
    runner._handle_goal_command = AsyncMock(return_value="controlled")

    before = get_goal_control_revision("session-1")
    result = await runner._busy_goal_command(
        event,
        "telegram:chat-1",
        "telegram:chat-1",
    )

    assert result == "controlled"
    assert get_goal_control_revision("session-1") == before + 1
    runner._handle_goal_command.assert_awaited_once_with(event)
    goals._DB_CACHE.clear()
