"""Named routes reach typed gateway config and real agent-construction boundaries."""
from copy import deepcopy

import pytest
import yaml

from gateway.config import Platform, load_gateway_config
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.model_presets import (
    ModelPresetError, expand_model_presets, preserve_model_preset_references,
)


@pytest.fixture
def authored(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = {
        "model_presets": {"local": {
            "provider": "custom:route", "model": "qwen:7b", "reasoning_effort": "low",
            "fallbacks": [{"provider": "custom:backup", "model": "backup:latest", "reasoning_effort": "high"}],
        }},
        "model": {"provider": "custom:global", "default": "global:latest"},
        "custom_providers": [
            {"name": name, "base_url": f"http://{name}.invalid/v1", "api_key": f"fixture-{name}"}
            for name in ("global", "route", "backup")
        ],
        "gateway": {"platforms": {
            "discord": {"channel_overrides": {"chan": {"model_preset": "local", "system_prompt": "Keep me."}}},
            "api_server": {"model_routes": {"alias": {"model_preset": "local"}}},
        }},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    return config


def test_config_route_roundtrip_and_validation(authored):
    expanded = expand_model_presets(authored)
    plat = expanded["gateway"]["platforms"]
    assert plat["api_server"]["model_routes"]["alias"]["model"] == "qwen:7b"
    assert plat["discord"]["channel_overrides"]["chan"]["fallbacks"] == authored["model_presets"]["local"]["fallbacks"]
    plat["discord"]["channel_overrides"]["chan"]["system_prompt"] = "Edited prompt."
    restored = preserve_model_preset_references(expanded, authored)
    expected = deepcopy(authored)
    expected["gateway"]["platforms"]["discord"]["channel_overrides"]["chan"]["system_prompt"] = "Edited prompt."
    assert restored == expected
    for field, value in (("model", "literal:latest"), ("base_url", "https://wrong.invalid"), ("api_key", "fixture-key")):
        invalid = deepcopy(authored)
        invalid["gateway"]["platforms"]["api_server"]["model_routes"]["alias"][field] = value
        with pytest.raises(ModelPresetError):
            expand_model_presets(invalid)
    invalid = deepcopy(authored)
    invalid["gateway"]["platforms"]["discord"]["channel_overrides"]["chan"]["model_preset"] = "missing"
    with pytest.raises(ModelPresetError, match="unknown preset"):
        expand_model_presets(invalid)


def test_typed_gateway_load_and_agent_routes(authored, monkeypatch):
    # Provider lookup, config loaders and preset expansion are real; no inference is sent.
    config = load_gateway_config()
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner._session_model_overrides = {}
    source = SessionSource(platform=Platform.DISCORD, chat_id="chan", user_id="user")
    model, runtime = runner._resolve_session_agent_runtime(source=source, user_config=expand_model_presets(authored))
    assert model == "qwen:7b"
    assert runtime["base_url"] == "http://route.invalid/v1"
    channel_route = runner._resolve_channel_route_config(source)
    route = runner._resolve_turn_agent_config("hello", model, runtime, channel_route=channel_route)
    assert route["fallback_model"] == authored["model_presets"]["local"]["fallbacks"]
    assert runner._resolve_session_reasoning_config(source=source, model=model, route=channel_route) == {"enabled": True, "effort": "low"}

    captured = {}
    class Agent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
    monkeypatch.setattr("run_agent.AIAgent", Agent)
    adapter = APIServerAdapter(config.platforms[Platform.API_SERVER])
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)
    monkeypatch.setattr(adapter, "_session_model_override_for", lambda *_: None)
    selected = adapter._resolve_route("alias")
    adapter._create_agent(route=selected)
    assert captured["model"] == "qwen:7b"
    assert captured["base_url"] == "http://route.invalid/v1"
    assert captured["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert captured["fallback_model"] == authored["model_presets"]["local"]["fallbacks"]
    adapter._create_agent(route={**selected, "fallbacks": []}, model_options={"reasoning_effort": "high"})
    assert captured["fallback_model"] == []
    assert captured["reasoning_config"] == {"enabled": True, "effort": "high"}
    adapter._create_agent(route=selected, confirmed_runtime_lock=True)
    assert captured["fallback_model"] is None
    adapter._create_agent(route=selected, session_model="session:latest")
    assert captured["model"] == "session:latest"
    assert captured["fallback_model"] is None
    assert captured["reasoning_config"] != {"enabled": True, "effort": "low"}
    monkeypatch.setattr(adapter, "_session_model_override_for", lambda *_: {"provider": "custom:global", "model": "session:latest"})
    adapter._create_agent(route=selected, session_id="override")
    assert captured["model"] == "session:latest"
    assert captured["base_url"] == "http://global.invalid/v1"
    assert captured["fallback_model"] is None
    runner._session_model_overrides = {"override": {"provider": "custom:global", "model": "session:latest"}}
    model, runtime = runner._resolve_session_agent_runtime(source=source, session_key="override", user_config=expand_model_presets(authored))
    assert model == "session:latest"
    assert not runner._resolve_channel_route_config(source, "override")
    runner._set_session_reasoning_override("override", {"enabled": False})
    assert runner._resolve_session_reasoning_config(session_key="override", model=model, route={"reasoning_effort": "high"}) == {"enabled": False}


def test_bad_reference_fails_gateway_startup(authored, tmp_path):
    authored["gateway"]["platforms"]["api_server"]["model_routes"]["alias"] = {"model_preset": "missing"}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(authored))
    with pytest.raises(ModelPresetError, match="unknown preset"):
        load_gateway_config()


@pytest.mark.parametrize("placement", ["platforms", "gateway", "extra", "top_channel"])
def test_platform_route_config_spellings(authored, tmp_path, placement):
    blocks = authored["gateway"]["platforms"]
    if placement == "platforms":
        authored["platforms"] = blocks
        authored.pop("gateway")
    elif placement == "gateway":
        authored["gateway"] = blocks
    elif placement == "top_channel":
        authored["discord"] = blocks.pop("discord")
    else:
        api = blocks["api_server"]
        api["extra"] = {"model_routes": api.pop("model_routes")}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(authored))
    loaded = load_gateway_config()
    assert loaded.platforms[Platform.DISCORD].channel_overrides["chan"].model == "qwen:7b"
    assert APIServerAdapter(loaded.platforms[Platform.API_SERVER])._resolve_route("alias")["model"] == "qwen:7b"
    assert preserve_model_preset_references(expand_model_presets(authored), authored) == authored


def test_config_save_keeps_route_references(authored, tmp_path):
    from hermes_cli.config import load_config, save_config
    loaded = load_config()
    loaded["display"]["personality"] = "concise"
    save_config(loaded)
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert raw["gateway"]["platforms"] == authored["gateway"]["platforms"]
    assert load_gateway_config().platforms[Platform.DISCORD].channel_overrides["chan"].model == "qwen:7b"


def test_channel_agent_cache_keeps_route_fallbacks(authored, monkeypatch):
    import threading
    from collections import OrderedDict
    from types import SimpleNamespace
    from gateway.run_turn_runner import TurnRunner

    runner = object.__new__(GatewayRunner)
    runner.config = load_gateway_config()
    runner._session_model_overrides = {}
    runner._session_db = None
    runner._service_tier = None
    runner._prefill_messages = []
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    monkeypatch.setattr(runner, "_enforce_agent_cache_cap", lambda: None)
    monkeypatch.setattr(runner, "_init_cached_agent_for_turn", lambda *_: None)
    def no_global_fallback():
        raise AssertionError("Selected route must not inherit global fallbacks")
    monkeypatch.setattr(runner, "_refresh_fallback_model", no_global_fallback)
    class Agent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._fallback_chain = kwargs["fallback_model"]
    source = SessionSource(platform=Platform.DISCORD, chat_id="chan", user_id="user")
    cfg = expand_model_presets(authored)
    ctx = SimpleNamespace(user_config=cfg, source=source, AIAgent=Agent, session_key="cache",
                          session_id="cache", enabled_toolsets=[], disabled_toolsets=[], _interrupt_depth=0)
    turn = TurnRunner(runner, ctx)
    model, runtime = runner._resolve_session_agent_runtime(source=source, user_config=cfg)
    channel_route = runner._resolve_channel_route_config(source)
    route = runner._resolve_turn_agent_config("hi", model, runtime, channel_route=channel_route)
    reasoning = runner._resolve_session_reasoning_config(source=source, model=model, route=channel_route)
    agent, reused = turn._resolve_turn_agent(route, "discord", "", 10, reasoning, {})
    assert not reused
    assert agent.kwargs["model"] == "qwen:7b"
    assert agent.kwargs["base_url"] == "http://route.invalid/v1"
    assert agent.kwargs["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert agent._fallback_chain == authored["model_presets"]["local"]["fallbacks"]
    route["fallback_model"] = []
    again, reused = turn._resolve_turn_agent(route, "discord", "", 10, reasoning, {})
    assert reused and again is agent
    assert agent._fallback_chain == []


@pytest.mark.asyncio
async def test_channel_runtime_remains_valid_for_compression_factories(authored, monkeypatch):
    import inspect
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from run_agent import AIAgent

    # Validate the real constructor signature, stopping before SDK/session initialization.
    signature = inspect.signature(AIAgent.__init__)
    class Agent:
        def __init__(self, **kwargs):
            signature.bind(self, **kwargs)
            self.kwargs = kwargs
    monkeypatch.setattr("run_agent.AIAgent", Agent)
    runner = object.__new__(GatewayRunner)
    runner.config = load_gateway_config()
    runner._session_model_overrides = {}
    runner._session_db = SimpleNamespace(get_session=AsyncMock(return_value=None))
    source = SessionSource(platform=Platform.DISCORD, chat_id="chan", user_id="user")
    model, runtime = runner._resolve_session_agent_runtime(source=source, user_config=expand_model_presets(authored))
    manual = await runner._build_manual_compression_agent("session", model, runtime)
    hygiene, _ = await runner._hmwa_hygiene_build_agent(model, runtime, SimpleNamespace(session_id="session"))
    for agent in (manual, hygiene):
        assert agent.kwargs["model"] == "qwen:7b"
        assert agent.kwargs["base_url"] == "http://route.invalid/v1"


def test_top_level_channel_reference_is_fail_closed(authored, tmp_path):
    authored["discord"] = {"channel_overrides": {"chan": {"model_preset": "missing"}}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(authored))
    with pytest.raises(ModelPresetError, match="discord.channel_overrides.chan"):
        load_gateway_config()


def test_named_api_route_does_not_borrow_default_credentials(authored, tmp_path):
    from gateway.platforms.api_server import _ProviderAuthResolutionError
    authored["model_presets"]["local"]["provider"] = "custom:unconfigured"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(authored))
    adapter = APIServerAdapter(load_gateway_config().platforms[Platform.API_SERVER])
    with pytest.raises(_ProviderAuthResolutionError):
        adapter._create_agent(route=adapter._resolve_route("alias"))
