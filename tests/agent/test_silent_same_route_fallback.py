"""A named alias of the active route is a retry, not a user-visible model switch."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import hermes_yaml as yaml
import pytest
from openai import InternalServerError
from anthropic.types import Message, TextBlock, Usage

from hermes_cli.runtime_provider import resolve_runtime_provider
from run_agent import AIAgent


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("fallback fixture must not make a network connection")

    monkeypatch.setattr("socket.socket.connect", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)


@pytest.mark.parametrize("change,inline,expected_notices", [
    ("none", False, 0),
    ("none", True, 0),
    ("credential", False, 0),
    ("model", False, 1),
    ("port", False, 1),
    ("wire", False, 1),
])
def test_resolved_fallback_retry_notice_contract(tmp_path, monkeypatch, caplog, change, inline, expected_notices):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    endpoint = "http://127.0.0.1:8317/v1"
    model = "claude-opus-5-5"
    fallback_model = "gpt-6.1-sol" if change == "model" else model
    fallback_endpoint = "http://127.0.0.1:8318/v1" if change == "port" else endpoint
    fallback_wire = "chat_completions" if change == "wire" else "anthropic_messages"
    fallback_provider = "custom:backup" if change in {"port", "wire", "credential"} else "custom:claude-proxy"
    config = {
        "model": {"provider": "custom:claude-proxy", "default": model},
        "providers": {
            "claude-proxy": {"api": endpoint, "transport": "anthropic_messages", "api_key": "primary-fixture"},
            "backup": {"api": fallback_endpoint, "transport": fallback_wire, "api_key": "fallback-fixture"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    runtime = resolve_runtime_provider(requested="custom:claude-proxy", target_model=model)
    assert (runtime["provider"], runtime["base_url"], runtime["api_mode"]) == (
        "custom", endpoint, "anthropic_messages")
    entry = {"provider": fallback_provider, "model": fallback_model, "api_mode": fallback_wire}
    if inline:
        entry["base_url"] = fallback_endpoint
    emitted, requested = [], []
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.model_metadata._probe_local_context_length", return_value=1_000_000),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
    ):
        agent = AIAgent(**{key: runtime[key] for key in ("provider", "api_key", "base_url", "api_mode")},
                        model=model, quiet_mode=True, skip_context_files=True,
                        skip_memory=True, fallback_model=[entry])
    agent._cached_system_prompt = "Model: claude-opus-5-5\nProvider: custom"
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._api_max_retries = 3
    agent._disable_streaming = True
    agent.status_callback = lambda _kind, message: emitted.append(message)

    def request(_kwargs):
        requested.append((agent.provider, agent.model, agent.base_url, agent.api_mode))
        if agent.provider == "custom" and len(requested) <= 3:
            response = httpx.Response(500, request=httpx.Request("POST", endpoint))
            raise InternalServerError("fixture server error", response=response, body={})
        if agent.api_mode == "anthropic_messages":
            return Message(id="fixture", type="message", role="assistant", model=agent.model,
                           content=[TextBlock(type="text", text="rescued")], stop_reason="end_turn",
                           usage=Usage(input_tokens=1, output_tokens=1))
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="rescued", tool_calls=None), finish_reason="stop")], usage=None)

    try:
        with (
            caplog.at_level("INFO"),
            patch.object(agent, "_interruptible_api_call", side_effect=request),
            patch("agent.turn_api_error.interruptible_backoff_sleep", return_value=None),
            patch("agent.turn_recovery_autorecover.auto_recover_after_exhaustion", return_value=None),
            patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
            patch("agent.model_metadata._probe_local_context_length", return_value=1_000_000),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("reply briefly")
            assert result["final_response"] == "rescued"
            assert not result.get("failed")
            assert [route[0] for route in requested] == ["custom"] * 3 + [fallback_provider]
            assert (requested[-1][1], requested[-1][2].rstrip("/"), requested[-1][3]) == (
                fallback_model, fallback_endpoint, fallback_wire)
            notices = [message for message in emitted if "Model fallback" in message]
            assert len(notices) == expected_notices
            assert "Fallback activated" in caplog.text
            emitted.clear()
            second_turn = agent.run_conversation("reply again")
            assert second_turn["final_response"] == "rescued"
            assert requested[-1][0] == "custom"
            assert len([message for message in emitted if "Primary model restored" in message]) == expected_notices
    finally:
        agent.close()


def test_notice_identity_excludes_url_credentials_but_preserves_route_axes():
    from agent.fallback_notice import resolved_route

    def route(endpoint, model="m", wire="anthropic_messages"):
        return resolved_route(SimpleNamespace(model=model, base_url=endpoint, api_mode=wire))

    assert route("https://user:old@proxy.invalid/v1/") == route("https://other:new@PROXY.invalid/v1")
    base = route("http://proxy.invalid:8317/API/v1?route=A")
    assert base != route("http://proxy.invalid:8318/API/v1?route=A")
    assert base != route("http://proxy.invalid:8317/api/v1?route=A")
    assert base != route("http://proxy.invalid:8317/API/v1?route=a")
    assert route("http://proxy.invalid/v1?") != route("http://proxy.invalid/v1")


def test_silent_alias_after_real_switch_keeps_recovery_notice_provenance():
    from agent.fallback_notice import buffer_fallback_route_notice, resolved_route
    from agent.error_classifier import FailoverReason

    agent = SimpleNamespace(model="m", base_url="http://proxy.invalid/v1", api_mode="anthropic_messages",
                            provider="custom:alias", _provider_fallback_active=True,
                            _provider_fallback_silent_retry=False)
    buffer_fallback_route_notice(agent, FailoverReason.server_error, False,
                                "m", "custom", resolved_route(agent), None)
    assert agent._provider_fallback_silent_retry is False


@pytest.mark.parametrize("silent", [True, False])
def test_shared_recovery_clears_state_without_announcing_a_silent_retry(tmp_path, monkeypatch, silent):
    from agent.shared_primary_cooldown import arm_cooldown, complete_primary_recovery, get_cooldown

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    route = ("custom", "http://127.0.0.1:8317/v1", "claude-opus-5-5")
    record = arm_cooldown(route, reason="overloaded")
    emitted = []
    agent = SimpleNamespace(
        provider=route[0], base_url=route[1], model=route[2], _fallback_activated=False,
        _shared_primary_cooldown_record=record, _provider_fallback_silent_retry=silent,
        _emit_diagnostic_status=emitted.append)
    assert complete_primary_recovery(agent)
    assert get_cooldown(route) is None
    assert agent._shared_primary_cooldown_record is None
    assert agent._rate_limited_until == 0
    assert len(emitted) == (0 if silent else 1)
