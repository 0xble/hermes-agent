"""Fallback notices distinguish credential surfaces and do not outlive their episode."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason
from agent.shared_primary_cooldown import complete_primary_recovery
from run_agent import AIAgent


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def refuse(*args, **kwargs):
        raise AssertionError("fallback notice fixture must not use the network")

    monkeypatch.setattr("socket.socket.connect", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.model_metadata._probe_local_context_length", return_value=128_000),
    ):
        instance = AIAgent(
            provider="xai-oauth", model="grok-4.5", api_mode="codex_responses",
            base_url="https://api.x.ai/v1", api_key="oauth-fixture",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            fallback_model=[{"provider": "xai", "model": "grok-4.5",
                             "base_url": "https://api.x.ai/v1", "api_mode": "codex_responses",
                             "api_key": "api-key-fixture"}],
        )
    try:
        yield instance
    finally:
        instance.close()


def test_first_class_credential_switch_and_verified_recovery_both_notify(agent):
    emitted = []
    agent.status_callback = lambda kind, message: emitted.append(message)
    # Keep resolver, binder, cooldown persistence and notice delivery real. Only clients are inert.
    with patch("agent.auxiliary_client.OpenAI", return_value=MagicMock(
        base_url="https://api.x.ai/v1", api_key="api-key-fixture")):
        assert agent._try_activate_fallback(FailoverReason.billing)
    agent._emit_pending_fallback_notice()
    assert (agent.provider, agent.model, agent.api_mode) == ("xai", "grok-4.5", "codex_responses")
    assert len([message for message in emitted if "using grok-4.5 via xai" in message]) == 1
    assert agent._provider_fallback_silent_retry is False
    emitted.clear()
    record = agent._shared_primary_cooldown_record
    with (
        patch("agent.shared_primary_cooldown.time.time", return_value=record["reset_at"] + 1),
        patch("agent.agent_runtime_helpers._rebuild_primary_client"),
    ):
        assert agent._restore_primary_runtime()
        assert not emitted  # shared recovery waits for a successful primary response
        assert complete_primary_recovery(agent)
    assert len([message for message in emitted if "Primary model restored" in message]) == 1


def _restore(agent):
    agent._fallback_activated = agent._provider_fallback_active = True
    with patch("agent.agent_runtime_helpers._rebuild_primary_client"):
        assert agent._restore_primary_runtime()


def _shared_restore(agent):
    from agent.shared_primary_cooldown import arm_cooldown, route_from_agent

    record = arm_cooldown(route_from_agent(agent), reason="overloaded")
    agent._shared_primary_cooldown_record = record
    agent._fallback_activated = agent._provider_fallback_active = True
    emitted = []
    agent._emit_diagnostic_status = emitted.append
    with (
        patch("agent.shared_primary_cooldown.time.time", return_value=record["reset_at"] + 1),
        patch("agent.agent_runtime_helpers._rebuild_primary_client"),
    ):
        assert agent._restore_primary_runtime()
        assert complete_primary_recovery(agent)
    assert not emitted  # consume this episode's silence without leaking it to the next


def _shared_recovery(agent):
    from agent.shared_primary_cooldown import arm_cooldown, route_from_agent

    agent._shared_primary_cooldown_record = arm_cooldown(route_from_agent(agent), reason="overloaded")
    assert complete_primary_recovery(agent)


def _model_switch(agent):
    from agent.agent_runtime_helpers import _finish_switch

    _finish_switch(agent, agent.provider, agent.provider, agent.provider)


def _inactive_exhaustion(agent):
    from agent.chat_completion_helpers import _fallback_chain_exhausted

    assert not getattr(agent, "_provider_fallback_active", False)
    assert not _fallback_chain_exhausted(agent, FailoverReason.server_error)


@pytest.mark.parametrize("shared", [False, True], ids=["local", "shared-record"])
def test_active_alias_exhaustion_preserves_silence_through_next_turn_restore(agent, shared):
    from agent.chat_completion_helpers import _fallback_chain_exhausted
    from agent.shared_primary_cooldown import arm_cooldown, get_cooldown, route_from_agent

    agent._primary_runtime["provider"] = "custom:primary"
    agent.provider = "custom:alias"
    agent._fallback_activated = agent._provider_fallback_active = True
    agent._provider_fallback_silent_retry = True
    agent._provider_fallback_route = (agent.model, agent.provider)
    route = route_from_agent(agent)
    record = arm_cooldown(route, reason="overloaded") if shared else None
    agent._shared_primary_cooldown_record = record
    emitted = []
    agent._emit_diagnostic_status = emitted.append

    assert not _fallback_chain_exhausted(agent, FailoverReason.server_error)
    assert agent._provider_fallback_active
    assert agent.provider == "custom:alias"
    assert agent._provider_fallback_silent_retry is True

    with (
        patch("agent.agent_runtime_helpers.time.monotonic", return_value=agent._rate_limited_until + 1),
        patch("agent.shared_primary_cooldown.time.time", return_value=(record or {}).get("reset_at", 0) + 1),
        patch("agent.agent_runtime_helpers._rebuild_primary_client"),
    ):
        assert agent._restore_primary_runtime()
        assert agent.provider == "custom:primary"
        assert not agent._provider_fallback_active
        assert agent._provider_fallback_silent_retry is False
        assert not emitted
        if shared:
            assert isinstance(agent._shared_primary_cooldown_record, dict)
            assert agent._shared_primary_cooldown_record["_silent_retry"] is True
            assert complete_primary_recovery(agent)
            assert get_cooldown(route) is None
            assert agent._shared_primary_cooldown_record is None
    assert not any("Primary model restored" in message for message in emitted)


def _voice(agent):
    from agent.voice_turn_route import _capture, _reinstall

    agent._provider_fallback_silent_retry = False
    state = _capture(agent)
    state.update({name: getattr(agent, name, None) for name in (
        "reasoning_config", "reasoning_override", "_pre_fallback_reasoning_override")})
    agent._provider_fallback_silent_retry = True  # a silent retry on the temporary voice route
    with patch("agent.agent_runtime_helpers._rebuild_primary_client"):
        _reinstall(agent, state)


@pytest.mark.parametrize("reset", [_restore, _shared_restore, _shared_recovery, _model_switch, _inactive_exhaustion, _voice])
def test_silent_provenance_cannot_leak_into_a_later_recovery(agent, reset):
    from agent.shared_primary_cooldown import arm_cooldown, route_from_agent

    agent._provider_fallback_silent_retry = True
    reset(agent)
    assert agent._provider_fallback_silent_retry is False
    emitted = []
    agent._emit_diagnostic_status = emitted.append
    agent._shared_primary_cooldown_record = arm_cooldown(route_from_agent(agent), reason="overloaded")
    assert complete_primary_recovery(agent)
    assert len(emitted) == 1
    assert "Primary model restored" in emitted[0]


@pytest.mark.parametrize("provider,skip", [("custom:primary", True), ("custom:alias", False)])
def test_deployment_skip_allows_only_different_label_alias_retry(provider, skip):
    from agent.chat_completion_helpers import _should_skip_fallback_candidate

    agent = SimpleNamespace(provider="custom:primary", model="m", base_url="http://proxy.invalid/v1")
    entry = {"provider": provider, "model": "m", "base_url": agent.base_url}
    with (
        patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None),
        patch("agent.chat_completion_helpers._candidate_pool_exhausted", return_value=False),
    ):
        assert _should_skip_fallback_candidate(agent, entry, (provider, "m"), provider, "m", set()) is skip
