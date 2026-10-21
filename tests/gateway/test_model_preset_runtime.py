"""Gateway primary-route model-preset regression coverage."""

from __future__ import annotations

import copy

import pytest

yaml = pytest.importorskip("yaml")


def test_gateway_primary_session_expands_preset_for_model_provider_reasoning_and_context(tmp_path, monkeypatch):
    """The raw config passed into a primary gateway turn is expanded at the runtime boundary."""
    import socket

    def deny_network(*args, **kwargs):
        raise AssertionError("Gateway route resolution must not require network access")

    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    from gateway import run as gateway_run
    from gateway.config import GatewayConfig
    from hermes_cli.model_presets import ModelPresetError

    home = tmp_path / "hermes-home"
    home.mkdir()
    authored = {
        "model_presets": {
            "primary": {
                "provider": "custom:gateway-test",
                "model": "gateway-primary-model",
                "reasoning_effort": "high",
            },
        },
        "model": {"model_preset": "primary", "context_length": 65432},
        "providers": {"gateway-test": {
            "base_url": "https://gateway-test.invalid/v1",
            "api_key": "test-key",
            "api_mode": "chat_completions",
        }},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(authored), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_run, "_hermes_home", home)

    raw = gateway_run._load_gateway_config()
    assert raw == authored

    runner = gateway_run.GatewayRunner(GatewayConfig())
    model, runtime = runner._resolve_session_agent_runtime(user_config=raw)

    assert model == "gateway-primary-model"
    assert runtime["provider"] == "custom"
    assert runtime["requested_provider"] == "custom:gateway-test"
    assert runtime["base_url"] == "https://gateway-test.invalid/v1"
    assert runtime["api_mode"] == "chat_completions"
    assert runtime["api_key"] == "test-key"
    assert not runner._refresh_fallback_model()
    assert gateway_run.GatewayRunner._load_reasoning_config(model) == {"enabled": True, "effort": "high"}
    assert gateway_run._resolve_gateway_model() == model
    assert gateway_run._resolve_gateway_model(raw) == model
    assert raw == authored

    captured = {}

    def fake_context_length(model, **kwargs):
        captured.update(model=model, **kwargs)
        return kwargs["config_context_length"]

    monkeypatch.setattr("agent.model_metadata.get_model_context_length", fake_context_length)
    context = gateway_run._resolve_gateway_model_context()
    assert (context.model, context.provider, context.context_length, context.context_source) == (
        "gateway-primary-model", "custom", 65432, "config",
    )
    assert captured["provider"] == "custom"

    unknown = copy.deepcopy(authored)
    unknown["model"] = {"model_preset": "missing"}
    with pytest.raises(ModelPresetError, match="model: unknown preset 'missing'"):
        gateway_run._resolve_gateway_model(unknown)


def test_gateway_string_at_model_reaches_session_runtime_with_provider(tmp_path, monkeypatch):
    from gateway import run as gateway_run
    from gateway.config import GatewayConfig

    home = tmp_path / "hermes-home"
    home.mkdir()
    authored = {
        "model_presets": {"coding": {"provider": "custom:gateway-test", "model": "coding-model"}},
        "model": "@coding",
        "providers": {"gateway-test": {
            "base_url": "https://gateway-test.invalid/v1", "api_key": "test-key", "api_mode": "chat_completions",
        }},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(authored), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_run, "_hermes_home", home)

    runner = gateway_run.GatewayRunner(GatewayConfig())
    model, runtime = runner._resolve_session_agent_runtime(user_config=gateway_run._load_gateway_config())

    assert model == "coding-model"
    assert runtime["requested_provider"] == "custom:gateway-test"
    assert runtime["base_url"] == "https://gateway-test.invalid/v1"


def test_gateway_resolves_at_reference_string_and_provider():
    from gateway import run as gateway_run

    config = {
        "model_presets": {"coding": {"provider": "openrouter", "model": "coding-model"}},
        "model": "@coding",
    }
    assert gateway_run._resolve_gateway_model(config) == "coding-model"


def test_gateway_resolves_model_mapping_at_reference_string():
    from gateway import run as gateway_run

    config = {
        "model_presets": {"coding": {"provider": "openrouter", "model": "coding-model"}},
        "model": {"default": "@coding"},
    }
    assert gateway_run._resolve_gateway_model(config) == "coding-model"


def test_gateway_explicit_context_rejects_invalid_preset(tmp_path, monkeypatch):
    from gateway import run as gateway_run
    from hermes_cli.model_presets import ModelPresetError

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("model:\n  model_preset: missing\n")
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *args, **kwargs: 8192)
    with pytest.raises(ModelPresetError, match="unknown preset 'missing'"):
        gateway_run._resolve_gateway_model_context("explicit-model")

