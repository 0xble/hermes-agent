"""Request-usage ledger coverage for truncation continuation responses."""

from unittest.mock import patch

from tests.agent.test_run_agent import _mock_response, agent as run_agent_fixture


def test_length_finish_reason_requests_continuation_records_each_response(request):
    agent = request.getfixturevalue("run_agent_fixture")
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    first = _mock_response(content="Part 1 ", finish_reason="length")
    second = _mock_response(content="Part 2", finish_reason="stop")
    agent.client.chat.completions.create.side_effect = [first, second]

    with (
        patch("hermes_cli.observability.request_usage_ledger.note_response") as note_response,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("hello")

    assert result["completed"] is True
    assert result["api_calls"] == 2
    assert result["final_response"] == "Part 1 Part 2"
    assert note_response.call_count == 2
    second_call_messages = agent.client.chat.completions.create.call_args_list[1].kwargs["messages"]
    assert second_call_messages[-1]["role"] == "user"
