"""End-to-end model-callable goal activation through AIAgent."""

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
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "test",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def test_goal_call_persists_and_same_turn_starts_work(tmp_path, monkeypatch):
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
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.session_id = "e2e-self-starting-goal"
    agent.tools = get_tool_definitions(["goal"])
    agent.valid_tool_names = {"set_goal"}
    agent.enabled_toolsets = ["goal"]

    request = "Set a goal to implement the parser and validate it works."
    tool_call = _tool_call(
        "set_goal",
        {
            "goal": "Implement the parser and validate it works",
            "authorization_text": request,
            "max_turns": 6,
        },
    )
    agent.client.chat.completions.create.side_effect = [
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
    assert state.goal == "Implement the parser and validate it works"
    assert state.max_turns == 6

    second_messages = agent.client.chat.completions.create.call_args_list[1].kwargs["messages"]
    roles = [message["role"] for message in second_messages]
    assert all(left != right for left, right in zip(roles[1:], roles[2:]))
    tool_result = next(
        message["content"] for message in second_messages if message["role"] == "tool"
    )
    receipt = json.loads(tool_result)
    assert receipt["success"] is True
    assert receipt["goal"] == state.goal

    goals._DB_CACHE.clear()
