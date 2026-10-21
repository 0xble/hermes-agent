"""Gateway primary-route model-preset regression coverage."""

from __future__ import annotations

import copy

import pytest

import hermes_yaml as yaml


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



def test_gateway_model_context_read_other_failure_fails_open(tmp_path, monkeypatch):
    from gateway import run as gateway_run
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: (_ for _ in ()).throw(OSError("offline")))
    context = gateway_run._resolve_gateway_model_context("explicit-model")
    assert context.model == "explicit-model"


def test_gateway_string_at_model_keeps_preset_reasoning_effort():
    from gateway.run_config_loaders import GatewayConfigLoadersMixin
    cfg = {"model_presets": {"primary": {"provider": "openrouter", "model": "m", "reasoning_effort": "high"}},
           "model": "@primary"}
    assert GatewayConfigLoadersMixin._load_reasoning_config(config=cfg) == {"enabled": True, "effort": "high"}


def test_gateway_model_resolver_passes_literal_at_model_id_through():
    from gateway.run import _resolve_gateway_model
    cf = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    assert _resolve_gateway_model({"model": cf}) == cf
    assert _resolve_gateway_model({"model": {"default": cf, "provider": "custom",
                                              "base_url": "https://cf.invalid/v1"}}) == cf


def _reasoning_cfg(**preset):
    return {
        "model": {"model_preset": "p"},
        "model_presets": {"p": {"provider": "openrouter", "model": "m-1", **preset}},
        "agent": {"reasoning_effort": "low", "reasoning_overrides": {"m-1": "high"}},
    }


def test_reasoning_config_preset_effort_outranks_per_model_override_and_global():
    from gateway.run import GatewayRunner
    result = GatewayRunner._load_reasoning_config("m-1", config=_reasoning_cfg(reasoning_effort="minimal"))
    assert result == {"enabled": True, "effort": "minimal"}


def test_reasoning_config_preset_without_effort_defers_to_per_model_override():
    from gateway.run import GatewayRunner
    result = GatewayRunner._load_reasoning_config("m-1", config=_reasoning_cfg())
    assert result == {"enabled": True, "effort": "high"}
    cfg = _reasoning_cfg(); cfg["agent"]["reasoning_overrides"] = {}
    assert GatewayRunner._load_reasoning_config("m-1", config=cfg) == {"enabled": True, "effort": "low"}


def test_reasoning_config_propagates_invalid_preset_error():
    from gateway.run import GatewayRunner
    from hermes_cli.model_presets import ModelPresetError
    cfg = {"model": {"model_preset": "missing"}, "model_presets": {}}
    with pytest.raises(ModelPresetError):
        GatewayRunner._load_reasoning_config("", config=cfg)


def test_reasoning_global_with_preset_default_loads_and_names_the_preset(tmp_path, monkeypatch):
    """`/reasoning high --global` while a preset owns the default must not break config load.

    The global value is saved, and the reply says the preset sets the main route's effort
    and that the saved value applies elsewhere.
    """
    import asyncio
    from types import SimpleNamespace

    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from hermes_cli.config import load_config

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": "@coding",
        "model_presets": {"coding": {"provider": "openrouter", "model": "coding-model", "reasoning_effort": "medium"}},
        "agent": {"reasoning_effort": "low"},
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("gateway.run._hermes_home", home)
    monkeypatch.setattr("gateway.run._gateway_config_home", lambda: home)

    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._set_reasoning_override = lambda *args, **kwargs: None
    runner._set_session_reasoning_override = lambda *args, **kwargs: None
    runner._live_agent_for_session_control = lambda _key: None
    runner._evict_cached_agent = lambda _key: None
    runner._evict_idle_agent_after_session_control = lambda _key: None
    runner._session_state = lambda _key: SimpleNamespace()
    runner.config_path = home / "config.yaml"
    runner._load_show_reasoning = lambda: False
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda _source: "agent:main:telegram:dm:1"
    event = MessageEvent(
        text="/reasoning high --global", message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, user_id="owner", chat_id="1", chat_type="dm"),
    )

    reply = asyncio.run(runner._handle_reasoning_command(event))

    loaded = load_config()
    assert loaded["model"]["default"] == "coding-model"
    assert loaded["agent"]["reasoning_effort"] == "high"
    assert "@coding" in reply
    assert "medium" in reply
    assert "high" in reply


def test_resolve_default_route_accepts_all_authored_shapes():
    from hermes_cli.model_preset_runtime import resolve_default_route

    preset = {"provider": "custom:test", "model": "preset-model", "reasoning_effort": "high"}
    for model in ("@primary", {"default": "@primary"}, {"model": "@primary"}, {"model_preset": "primary"}):
        route = resolve_default_route({"model_presets": {"primary": preset}, "model": model})
        assert (route.model, route.provider, route.reasoning_effort) == ("preset-model", "custom:test", "high")

    concrete = resolve_default_route({"model": {"default": "concrete-model", "provider": "openrouter", "base_url": "https://example.invalid"}})
    assert (concrete.model, concrete.provider, concrete.base_url) == (
        "concrete-model", "openrouter", "https://example.invalid",
    )


def test_model_switch_context_uses_preset_default_route(tmp_path, monkeypatch):
    from gateway.slash_commands_model import _ModelSwitchContext
    from gateway import run as gateway_run

    config = {
        "model_presets": {"primary": {"provider": "custom:test", "model": "preset-model"}},
        "model": {"model_preset": "primary"},
    }
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda config_path=None: config)
    ctx = _ModelSwitchContext("session", None, tmp_path / "config.yaml", False)
    ctx.read_config()
    assert (ctx.current_model, ctx.current_provider, ctx.route_provider) == (
        "preset-model", "custom:test", "custom:test",
    )
