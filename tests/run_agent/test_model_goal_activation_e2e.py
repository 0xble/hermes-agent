"""End-to-end model-callable autonomous goal activation through AIAgent."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _tool_call(name, arguments):
    return SimpleNamespace(
        id=f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(*, content, finish_reason, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _minimal_tools():
    return [{
        "type": "function",
        "function": {
            "name": "web_search", "description": "test",
            "parameters": {"type": "object", "properties": {}},
        },
    }]


def test_model_can_set_goal_and_same_turn_starts_work(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals
    from model_tools import get_tool_definitions

    goals._DB_CACHE.clear()
    with (
        patch("run_agent.get_tool_definitions", return_value=_minimal_tools()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
        )

    notices = []
    agent.notice_callback = notices.append
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.session_id = "e2e-self-starting-goal"
    agent.tools = get_tool_definitions(["goal"])
    agent.valid_tool_names = {"set_goal"}
    agent.enabled_toolsets = ["goal"]

    request = "Implement the parser and validate it works."
    tool_call = _tool_call("set_goal", {
        "goal": "Parser implementation passes compatibility and regression tests",
        "max_turns": 6,
    })
    agent.client.chat.completions.create.side_effect = [
        _response(content=None, finish_reason="tool_calls", tool_calls=[_tool_call("set_goal", {"action": "guide"})]),
        _response(content=None, finish_reason="tool_calls", tool_calls=[tool_call]),
        _response(
            content="I added the first failing parser test and am implementing it now.",
            finish_reason="stop",
        ),
    ]

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(request)

    assert result["completed"] is True
    assert agent._current_goal_control_revision == 0
    assert result["final_response"].startswith("I added the first failing parser test")
    state = goals.GoalManager(agent.session_id).state
    assert state is not None
    assert state.status == "active"
    assert state.goal == "Parser implementation passes compatibility and regression tests"
    assert state.max_turns == 6

    calls = agent.client.chat.completions.create.call_args_list
    from tools.goal_tool import GOAL_WRITING_GUIDANCE

    assert GOAL_WRITING_GUIDANCE not in json.dumps(calls[0].kwargs["messages"])
    assert GOAL_WRITING_GUIDANCE not in json.dumps(calls[0].kwargs.get("tools", []))
    guide_result = next(m["content"] for m in calls[1].kwargs["messages"] if m["role"] == "tool")
    assert json.loads(guide_result)["guidance"] == GOAL_WRITING_GUIDANCE
    second_messages = calls[2].kwargs["messages"]
    roles = [message["role"] for message in second_messages]
    assert all(left != right for left, right in zip(roles[1:], roles[2:]))
    tool_result = next(message["content"] for message in reversed(second_messages) if message["role"] == "tool")
    receipt = json.loads(tool_result)
    assert receipt["success"] is True
    assert receipt["goal"] == state.goal
    assert len(notices) == 1
    assert state.goal in notices[0].text
    assert "/goal status" in notices[0].text
    assert notices[0].text == receipt["notice"]

    goals._DB_CACHE.clear()
