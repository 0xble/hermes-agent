"""Startup capability reads preserve cached/configured answers without network probes."""
from types import SimpleNamespace

import pytest

from agent import image_routing


@pytest.mark.parametrize("supports", [True, False, None])
def test_cache_only_routing_uses_catalog_without_local_probes(monkeypatch, supports):
    from agent import models_dev
    from hermes_cli.local_runtime import capabilities
    from agent import model_metadata

    seen = []

    def catalog(provider, model, *, allow_network):
        seen.append(allow_network)
        return SimpleNamespace(supports_vision=supports)

    def unexpected(*args, **kwargs):
        pytest.fail("schema metadata attempted endpoint discovery")

    monkeypatch.setattr(models_dev, "get_model_capabilities", catalog)
    monkeypatch.setattr(capabilities, "managed_model_supports_vision", unexpected)
    monkeypatch.setattr(model_metadata, "query_ollama_supports_vision", unexpected)
    assert image_routing.decide_image_input_mode(
        "ollama", "fixture", {}, allow_network=False,
    ) == ("native" if supports else "text")
    assert seen == [False]


def test_cache_only_routing_honors_config_override(monkeypatch):
    monkeypatch.setattr(image_routing, "_VISION_PROBES", ())
    assert image_routing.decide_image_input_mode(
        "custom", "fixture", {"model": {"supports_vision": True}}, allow_network=False,
    ) == "native"


def test_actual_image_routing_keeps_network_discovery(monkeypatch):
    from agent import models_dev
    seen = []

    def catalog(provider, model, *, allow_network):
        seen.append(allow_network)
        return SimpleNamespace(supports_vision=False)

    monkeypatch.setattr(models_dev, "get_model_capabilities", catalog)
    assert image_routing.decide_image_input_mode("openai", "fixture", {}) == "text"
    assert seen == [True]


def test_tool_availability_disables_capability_network(monkeypatch):
    from agent import auxiliary_client
    from tools import vision_tools
    seen = []

    def resolve(**kwargs):
        seen.append((kwargs, auxiliary_client._aux_probe_active()))
        return None, None, None

    monkeypatch.setattr(auxiliary_client, "resolve_vision_provider_client", resolve)
    monkeypatch.setattr(vision_tools, "_should_use_native_vision_fast_path", lambda **kw: False)
    assert vision_tools.check_vision_requirements() is False
    assert seen == [({}, True), ({"provider": "auto"}, True)]


def test_auxiliary_capability_policy_reaches_catalog(monkeypatch):
    from agent import auxiliary_client
    from agent import models_dev
    monkeypatch.setattr(models_dev, "get_model_capabilities", lambda *args, **kw: (
        SimpleNamespace(supports_vision=False) if kw["allow_network"] is False else None
    ))
    with auxiliary_client.aux_probe_mode():
        assert auxiliary_client._main_model_supports_vision("openai", "fixture") is False
    assert auxiliary_client._main_model_supports_vision("openai", "fixture") is True


def test_browser_schema_uses_cached_native_vision_answer(monkeypatch):
    from agent import auxiliary_client, models_dev
    from hermes_cli import config
    from tools import browser_use_cli
    monkeypatch.setattr(auxiliary_client, "_read_main_provider", lambda: "openai")
    monkeypatch.setattr(auxiliary_client, "_read_main_model", lambda: "fixture")
    monkeypatch.setattr(config, "load_config", dict)
    seen = []

    def catalog(provider, model, *, allow_network):
        seen.append(allow_network)
        return SimpleNamespace(supports_vision=True)

    monkeypatch.setattr(models_dev, "get_model_capabilities", catalog)
    monkeypatch.setattr("tools.browser_tool_lightpanda_fallback.lightpanda_engine_status", lambda: (False, ""))
    assert browser_use_cli._description_header() == browser_use_cli._HEADER_BASE + browser_use_cli._HEADER_VISION
    assert seen and not any(seen)
