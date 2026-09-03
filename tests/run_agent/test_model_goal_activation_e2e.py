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
    agent.tools = get_tool_definitions(["goal"]) + _minimal_tools()
    agent.valid_tool_names = {"set_goal", "web_search"}
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
        _response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[_tool_call("web_search", {"query": "parser"})],
        ),
        _response(
            content="The first search failed, so work has not started yet.",
            finish_reason="stop",
        ),
        _response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[_tool_call("web_search", {"query": "parser retry"})],
        ),
        _response(
            content="I started the parser investigation.",
            finish_reason="stop",
        ),
    ]

    original_execute = agent._execute_tool_calls

    task_attempts = 0

    def execute_calls(assistant_message, messages, *args, **kwargs):
        nonlocal task_attempts
        calls = assistant_message.tool_calls or []
        if all(call.function.name == "set_goal" for call in calls):
            return original_execute(assistant_message, messages, *args, **kwargs)
        for call in calls:
            task_attempts += 1
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.function.name,
                    "content": json.dumps({"success": task_attempts > 1}),
                }
            )
        return None

    with (
        patch.object(agent, "_execute_tool_calls", side_effect=execute_calls),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(request)

    assert result["completed"] is True
    assert agent._current_goal_control_revision == 0
    assert result["final_response"] == "I started the parser investigation."
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
    assert agent.client.chat.completions.create.call_count == 6
    third_messages = agent.client.chat.completions.create.call_args_list[2].kwargs[
        "messages"
    ]
    assert any(
        "Execute the required tool calls" in str(message.get("content", ""))
        for message in third_messages
    )
    fifth_messages = agent.client.chat.completions.create.call_args_list[4].kwargs[
        "messages"
    ]
    assert any(
        "Execute the required tool calls" in str(message.get("content", ""))
        for message in fifth_messages
    )
    goals._DB_CACHE.clear()


def test_goal_activation_gets_one_grace_call_at_iteration_limit(tmp_path, monkeypatch):
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
    agent.session_id = "e2e-goal-activation-grace"
    agent.tools = get_tool_definitions(["goal"]) + _minimal_tools()
    agent.valid_tool_names = {"set_goal", "web_search"}
    agent.enabled_toolsets = ["goal"]
    agent.max_iterations = 1

    request = "Set a goal to investigate the parser and start now."
    activation_call = _tool_call(
        "set_goal",
        {
            "goal": "Investigate the parser",
            "authorization_text": request,
            "max_turns": 6,
        },
    )
    task_call = _tool_call("web_search", {"query": "parser"})
    agent.client.chat.completions.create.side_effect = [
        _response(content=None, finish_reason="tool_calls", tool_calls=[activation_call]),
        _response(content=None, finish_reason="tool_calls", tool_calls=[task_call]),
    ]

    original_execute = agent._execute_tool_calls
    task_started = False

    def execute_calls(assistant_message, messages, *args, **kwargs):
        nonlocal task_started
        calls = assistant_message.tool_calls or []
        if all(call.function.name == "set_goal" for call in calls):
            return original_execute(assistant_message, messages, *args, **kwargs)
        task_started = True
        for call in calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.function.name,
                    "content": json.dumps({"data": {"started": True}}),
                }
            )
        return None

    with (
        patch.object(agent, "_execute_tool_calls", side_effect=execute_calls),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        agent.run_conversation(request)

    assert task_started is True
    assert agent.client.chat.completions.create.call_count >= 2
    goals._DB_CACHE.clear()
