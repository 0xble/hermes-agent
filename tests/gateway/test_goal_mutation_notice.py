"""Persisted goal receipts reach the normal notice lane, not progress cleanup."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.inline_tool_executors import emit_terminal_post_tool_call
from agent.status_output import StatusOutputMixin
from gateway.config import Platform
from gateway.run_notifications import GatewayNotificationsMixin
from gateway.run_turn_runner import TurnRunner
from hermes_cli import goals
from hermes_cli.goal_display import format_goal_change
from tools.goal_tool import set_goal_tool


@pytest.mark.asyncio
async def test_mutation_receipt_reaches_origin_thread_and_keeps_full_text(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="chat", user_id="user")
    adapter = SimpleNamespace(send=AsyncMock())
    runner = GatewayNotificationsMixin()
    runner.config = None
    runner._adapter_for_source = lambda source: adapter
    runner._thread_metadata_for_source = lambda source: {"thread_id": "171859"}
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = SimpleNamespace(source=source, _status_adapter=adapter, _run_still_current=lambda: True)
    pending = []
    turn._schedule = lambda coro, label: pending.append(asyncio.create_task(coro))
    agent = StatusOutputMixin()
    agent.notice_callback = turn._notice_callback_sync
    agent.session_id = "notices"

    def invoke(**args):
        result = set_goal_tool(session_id="notices", turn_id="turn", goal_control_revision=0, **args)
        emit_terminal_post_tool_call(agent, function_name="set_goal", function_args=args,
                                    result=result, effective_task_id="task", tool_call_id="call")
        return json.loads(result)

    goal = ("Validate café parser " + "long complete objective " * 100).strip()
    first = invoke(action="set", goal=goal,
                   contract={"verification": "Parser tests pass", "boundaries": "No deployment"})
    assert first["success"] is True
    await asyncio.gather(*pending)
    adapter.send.assert_awaited_once_with("chat", first["notice"], metadata={"thread_id": "171859"})
    assert goal in first["notice"]
    assert "Parser tests pass" in first["notice"]
    assert "No deployment" in first["notice"]
    state = goals.load_goal("notices")
    assert state is not None
    assert first["notice"] == format_goal_change("set", state)

    pending.clear()
    edited = invoke(action="edit", goal="Parser fully validated")
    assert edited["success"] is True
    assert "Parser fully validated" in edited["notice"]
    assert "Parser tests pass" in edited["notice"]
    assert "No deployment" in edited["notice"]
    await asyncio.gather(*pending)
    assert adapter.send.await_count == 2

    pending.clear()
    assert invoke(action="status")["success"] is True
    assert invoke(action="guide")["success"] is True
    assert invoke(action="clear")["success"] is True
    await asyncio.gather(*pending)
    assert adapter.send.await_count == 3
    pending.clear()
    # Obsolete turns cannot publish into a newer session's thread.
    turn._ctx._run_still_current = lambda: False
    emit_terminal_post_tool_call(agent, function_name="set_goal", function_args={},
                                result=json.dumps(edited), effective_task_id="task", tool_call_id="stale")
    assert pending == []
    goals._DB_CACHE.clear()


def test_formatter_failure_does_not_claim_committed_mutation_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()

    def broken(*args, **kwargs):
        raise RuntimeError("renderer unavailable")

    monkeypatch.setattr("hermes_cli.goal_display.format_goal_change", broken)
    result = json.loads(set_goal_tool(
        action="set", goal="Parser validated", session_id="format-failure",
        turn_id="turn", goal_control_revision=0,
    ))
    assert result["success"] is True
    assert goals.load_goal("format-failure").goal == result["state"]["goal"]
    goals._DB_CACHE.clear()
