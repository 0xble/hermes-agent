"""Tests for per-model reasoning_effort override during /model switch.

Tests that switch_model:
1. Re-resolves reasoning_config when switching to a model with an override
2. Falls back to global when switching to a model without an override
3. Saves reasoning_config into _primary_runtime for fallback recovery
"""

from unittest.mock import MagicMock


class TestSwitchModelReasoningOverride:
    """Test switch_model re-resolves reasoning_config on model switch."""

    def _make_fake_agent(self, model="gpt-5", provider="openai"):
        """Create a minimal fake agent for switch_model testing."""
        agent = MagicMock()
        agent.model = model
        agent.provider = provider
        agent.base_url = "https://api.openai.com/v1"
        agent.api_mode = "openai"
        agent.api_key = "test-key"
        agent._client_kwargs = {"api_key": "test-key", "base_url": "https://api.openai.com/v1"}
        agent._use_prompt_caching = False
        agent._use_native_cache_layout = False
        agent.reasoning_config = {"enabled": True, "effort": "medium"}
        agent._fallback_activated = False
        agent._fallback_index = 0
        agent._fallback_chain = []
        agent._fallback_model = None
        agent._config_context_length = None
        agent._transport_cache = {}
        agent.context_compressor = None
        agent._cached_system_prompt = None
        agent._anthropic_api_key = ""
        agent._anthropic_base_url = None
        agent._is_anthropic_oauth = False
        agent._anthropic_prompt_cache_policy = MagicMock(
            return_value=(False, False)
        )
        agent._ensure_lmstudio_runtime_loaded = MagicMock()
        agent._create_openai_client = MagicMock(return_value=MagicMock())
        return agent




    def test_restore_primary_runtime_restores_reasoning(self):
        """restore_primary_runtime should restore reasoning_config from snapshot."""
        from agent.agent_runtime_helpers import restore_primary_runtime

        agent = MagicMock()
        agent._primary_runtime = {
            "model": "claude-opus-4.5",
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_mode": "anthropic_messages",
            "api_key": "key",
            "client_kwargs": {},
            "use_prompt_caching": True,
            "use_native_cache_layout": False,
            "reasoning_config": {"enabled": True, "effort": "xhigh"},
            "compressor_model": "claude-opus-4.5",
            "compressor_base_url": "",
            "compressor_api_key": "",
            "compressor_provider": "",
            "compressor_context_length": 0,
            "compressor_api_mode": "",
            "compressor_threshold_tokens": 0,
            "anthropic_api_key": "key",
            "anthropic_base_url": "https://api.anthropic.com",
            "is_anthropic_oauth": False,
        }
        agent._fallback_activated = True
        agent._fallback_index = 0
        agent._fallback_chain = []
        agent._fallback_model = None
        agent._transport_cache = {}
        agent._config_context_length = None
        agent._rate_limited_until = 0
        agent.model = "fallback-model"
        agent.provider = "openai"
        agent.reasoning_config = {"enabled": True, "effort": "medium"}
        agent.context_compressor = MagicMock()
        agent.base_url = ""
        # Mock the methods restore_primary_runtime calls
        agent._anthropic_prompt_cache_policy = MagicMock(return_value=(True, False))
        agent._create_openai_client = MagicMock(return_value=MagicMock())
        agent._ensure_lmstudio_runtime_loaded = MagicMock()

        result = restore_primary_runtime(agent)
        assert result is True
        assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}


    def test_restore_primary_runtime_restores_explicit_marker(self):
        """The explicit-pick marker travels with the primary's level through a fallback round trip."""
        from agent.agent_runtime_helpers import restore_primary_runtime

        xhigh = {"enabled": True, "effort": "xhigh"}
        agent = MagicMock()
        agent._primary_runtime = {
            "model": "claude-opus-4.5", "provider": "anthropic", "base_url": "https://api.anthropic.com",
            "api_mode": "anthropic_messages", "api_key": "key", "client_kwargs": {},
            "use_prompt_caching": True, "use_native_cache_layout": False,
            "reasoning_config": dict(xhigh),
            "compressor_model": "claude-opus-4.5", "compressor_base_url": "", "compressor_api_key": "",
            "compressor_provider": "", "compressor_context_length": 0, "compressor_api_mode": "",
            "compressor_threshold_tokens": 0, "anthropic_api_key": "key",
            "anthropic_base_url": "https://api.anthropic.com", "is_anthropic_oauth": False,
        }
        agent._pre_fallback_reasoning_override = dict(xhigh)  # set aside by fallback activation
        agent._fallback_activated = True
        agent._fallback_index = 0
        agent._fallback_chain = []
        agent._fallback_model = None
        agent._transport_cache = {}
        agent._config_context_length = None
        agent._rate_limited_until = 0
        agent.model = "fallback-model"
        agent.provider = "openai"
        agent.reasoning_config = {"enabled": True, "effort": "medium"}
        agent.reasoning_override = None  # fallback activation retired it
        agent.context_compressor = MagicMock()
        agent.base_url = ""
        agent._anthropic_prompt_cache_policy = MagicMock(return_value=(True, False))
        agent._create_openai_client = MagicMock(return_value=MagicMock())
        agent._ensure_lmstudio_runtime_loaded = MagicMock()

        assert restore_primary_runtime(agent) is True
        assert agent.reasoning_override == xhigh

    def test_restore_primary_runtime_restores_a_pick_newer_than_the_snapshot(self):
        """A snapshot taken before a live /reasoning pick must not pair the pick with its stale level."""
        from agent.agent_runtime_helpers import restore_primary_runtime

        xhigh = {"enabled": True, "effort": "xhigh"}
        agent = MagicMock()
        agent._primary_runtime = {
            "model": "claude-opus-4.5", "provider": "anthropic", "base_url": "https://api.anthropic.com",
            "api_mode": "anthropic_messages", "api_key": "key", "client_kwargs": {},
            "use_prompt_caching": True, "use_native_cache_layout": False,
            "reasoning_config": {"enabled": True, "effort": "medium"},  # configured, pre-pick
            "compressor_model": "claude-opus-4.5", "compressor_base_url": "", "compressor_api_key": "",
            "compressor_provider": "", "compressor_context_length": 0, "compressor_api_mode": "",
            "compressor_threshold_tokens": 0, "anthropic_api_key": "key",
            "anthropic_base_url": "https://api.anthropic.com", "is_anthropic_oauth": False,
        }
        agent._pre_fallback_reasoning_override = dict(xhigh)
        agent._fallback_activated = True
        agent._fallback_index = 0
        agent._fallback_chain = []
        agent._fallback_model = None
        agent._transport_cache = {}
        agent._config_context_length = None
        agent._rate_limited_until = 0
        agent.model = "fallback-model"
        agent.provider = "openai"
        agent.reasoning_config = {"enabled": True, "effort": "low"}
        agent.reasoning_override = None
        agent.context_compressor = MagicMock()
        agent.base_url = ""
        agent._anthropic_prompt_cache_policy = MagicMock(return_value=(True, False))
        agent._create_openai_client = MagicMock(return_value=MagicMock())
        agent._ensure_lmstudio_runtime_loaded = MagicMock()

        assert restore_primary_runtime(agent) is True
        assert agent.reasoning_config == xhigh
        assert agent.reasoning_override == xhigh

        from tools.delegate_tool_config import explicit_parent_reasoning
        assert explicit_parent_reasoning(agent) == xhigh


def test_switch_model_retires_explicit_marker_even_when_level_is_equal(monkeypatch):
    """A config re-resolution that lands on the SAME level as an explicit pick still retires it.

    Value equality alone cannot tell a stale pick from a live one, so without the reset an explicit
    ``xhigh`` would outlive a switch to a model whose configured level is also ``xhigh``.
    """
    import hermes_cli.config as config_mod
    from agent import agent_runtime_helpers as helpers

    xhigh = {"enabled": True, "effort": "xhigh"}
    monkeypatch.setattr(config_mod, "load_config", lambda: {"agent": {"reasoning_effort": "xhigh"}})
    agent = TestSwitchModelReasoningOverride()._make_fake_agent()
    agent.reasoning_config = dict(xhigh)
    agent.reasoning_override = dict(xhigh)
    try:
        helpers.switch_model(agent, new_model="gpt-5.1", new_provider="openai", api_key="k",
                             base_url="https://api.openai.com/v1", api_mode="chat_completions")
    except Exception:
        pass  # MagicMock agent: only the reasoning re-resolution step matters here
    assert agent.reasoning_config == xhigh
    assert agent.reasoning_override is None
