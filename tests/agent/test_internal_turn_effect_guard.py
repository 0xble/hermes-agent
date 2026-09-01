"""Synthetic continuation turns cannot originate control-plane mutations."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.tool_executor import (
    _internal_turn_effect_block,
    _run_agent_tool_execution_middleware,
)


def _agent(*, internal: bool):
    return SimpleNamespace(
        _current_turn_is_internal=internal,
        _current_turn_id="turn-incident",
        session_id="session-incident",
    )


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("delegate_task", {"tasks": [{"goal": "pause the cron"}]}),
        ("cronjob", {"action": "create"}),
        ("cronjob", {"action": "update"}),
        ("cronjob", {"action": "pause"}),
        ("cronjob", {"action": "resume"}),
        ("cronjob", {"action": "remove"}),
        ("cronjob", {"action": "run"}),
        ("memory", {"action": "add"}),
        ("memory", {"operations": [{"action": "add", "content": "x"}]}),
        ("send_message", {"action": "send"}),
        ("set_goal", {"goal": "keep working"}),
        ("skill_manage", {"operations": []}),
        ("hindsight_retain", {"content": "x"}),
        ("computer_use", {"action": "click"}),
    ],
)
def test_internal_turn_blocks_new_control_plane_effects(tool, args):
    result = _internal_turn_effect_block(_agent(internal=True), tool, args)

    assert result is not None
    assert "was not executed" in result
    assert "user-authored instruction" in result


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("cronjob", {"action": "list"}),
        ("memory", {"action": "list"}),
        ("computer_use", {"action": "capture"}),
        ("set_goal", {"action": "status"}),
        ("set_goal", {"action": "show"}),
        ("set_goal", {"action": "subgoal_list"}),
        ("set_goal", {"action": "gate_list"}),
        ("read_file", {"path": "README.md"}),
        ("terminal", {"command": "git status --short"}),
    ],
)
def test_internal_turn_preserves_read_and_project_work(tool, args):
    assert _internal_turn_effect_block(_agent(internal=True), tool, args) is None


def test_real_user_turn_can_mutate_control_plane():
    assert (
        _internal_turn_effect_block(
            _agent(internal=False),
            "cronjob",
            {"action": "pause", "job_id": "351d0887fb68"},
        )
        is None
    )


def test_common_execution_boundary_does_not_dispatch_blocked_effect():
    execute = MagicMock(return_value="should not run")
    agent = _agent(internal=True)
    agent._current_api_request_id = "request-incident"
    agent._touch_activity = lambda _label: None
    agent._guardrail_block_result = lambda _decision: "guardrail block"
    agent._tool_guardrails = SimpleNamespace(
        before_call=lambda _name, _args: SimpleNamespace(allows_execution=True)
    )

    outcome = _run_agent_tool_execution_middleware(
        agent,
        function_name="cronjob",
        function_args={"action": "pause", "job_id": "351d0887fb68"},
        effective_task_id="session-incident",
        tool_call_id="call-incident",
        execute=execute,
    )

    execute.assert_not_called()
    assert outcome.blocked is True
    assert json.loads(outcome.result)["error"].endswith(
        "A new user-authored instruction is required."
    )