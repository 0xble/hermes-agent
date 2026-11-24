"""Real-agent regression coverage for system-sourced machine-turn preemption."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from tests.agent.test_run_agent import _make_tool_defs


@pytest.fixture
def agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        result = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        object.__setattr__(result, "client", MagicMock())
        return result


def test_system_soft_interrupt_real_agent_records_source_without_followup(agent, monkeypatch):
    """The actual InterruptControlMixin/finalizer path keeps a machine stop system-sourced."""
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    request_client = MagicMock()

    def provider(*_args, **_kwargs):
        agent.interrupt(None, tool_reason="gateway user turn preemption")
        raise OSError("fixture request stopped")

    request_client.chat.completions.create.side_effect = provider
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **_kw: request_client)
    monkeypatch.setattr(agent, "_abort_request_openai_client", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda *_a, **_kw: None)

    with patch.object(agent, "_persist_session") as persist:
        result = agent.run_conversation("machine wake")

    assert result["interrupted"] is True
    assert result.get("interrupt_message") is None
    assert result["interrupt_source"] == "gateway_user_turn_preemption"
    assert not result.get("pending_steer")
    assert any(message.get("role") == "user" and message.get("content") == "machine wake"
               for message in result["messages"])
    persist.assert_called()
