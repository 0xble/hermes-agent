"""Named delegation definitions: validation before any runtime side effects."""

import pytest

from tools.custom_subagents import parse_definitions, resolve_definition


def definition(**overrides):
    return {
        "description": "Investigate with source references.",
        "instructions": "Do not modify the investigated target.",
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
        **overrides,
    }


def test_absent_registry_preserves_legacy():
    assert parse_definitions({}) == {}
    assert parse_definitions({"subagents": None}) == {}
    assert resolve_definition({}, None) is None


@pytest.mark.parametrize("name,model", [("explorer", "gpt-5.6-luna"), ("worker", "gpt-5.6-terra")])
def test_deployment_definitions_are_immutable(name, model):
    raw = definition(model=model)
    registry = parse_definitions({"subagents": {name: raw}})
    resolved = resolve_definition(registry, name)
    raw["model"] = "changed"
    assert resolved.model == model
    assert resolved.reasoning_effort == "medium"
    assert resolved.provider == "openai-codex"
    with pytest.raises((AttributeError, TypeError)):
        resolved.model = "changed"


@pytest.mark.parametrize("registry", [[], "worker", {"bad name": definition()}, {"": definition()}, {"worker": None}, {"worker": []}])
def test_malformed_registry_fails(registry):
    with pytest.raises(ValueError, match="delegation.subagents"):
        parse_definitions({"subagents": registry})


@pytest.mark.parametrize("overrides", [
    {"description": ""}, {"instructions": " "}, {"provider": ""},
    {"description": True}, {"instructions": []}, {"model": None},
    {"reasoning_effort": "typo"}, {"reasoning_effort": None},
    {"reasoning_effort": "MEDIUM"}, {"api_key": "not-a-secret"},
    {"toolsets": ["terminal"]}, {"max_iterations": 9},
])
def test_bad_definition_fields_fail(overrides):
    with pytest.raises(ValueError, match="delegation.subagents.worker"):
        parse_definitions({"subagents": {"worker": definition(**overrides)}})


def test_explicit_provider_requires_explicit_model():
    raw = definition()
    del raw["model"]
    with pytest.raises(ValueError, match="model"):
        parse_definitions({"subagents": {"worker": raw}})


def test_unknown_type_is_not_legacy_fallback():
    with pytest.raises(ValueError, match="Unknown subagent_type"):
        resolve_definition({}, "worker")


@pytest.mark.parametrize("selected", ["", " worker", [], 4, False])
def test_malformed_selection_fails(selected):
    with pytest.raises(ValueError, match="subagent_type"):
        resolve_definition({}, selected)


def test_named_codex_uses_parent_route_not_global_pin():
    from types import SimpleNamespace
    from tools.custom_subagents import resolve_named_credentials
    parent = SimpleNamespace(
        provider="openai-codex", model="gpt-6-astra",
        base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses", api_key="fixture-token",
        reasoning_config={"effort": "high"}, request_overrides={},
    )
    selected = parse_definitions({"subagents": {"worker": definition()}})["worker"]
    creds, reasoning = resolve_named_credentials(selected, {"provider": "openrouter", "api_key": "wrong-route"}, parent)
    assert creds["api_key"] == "fixture-token"
    assert creds["base_url"] == parent.base_url
    assert creds["model"] == "gpt-5.6-luna"
    assert reasoning == {"enabled": True, "effort": "medium"}
    assert parent.reasoning_config == {"effort": "high"}


@pytest.mark.parametrize("changes", [
    {"provider": "openai"}, {"base_url": "https://api.openai.com/v1"},
    {"base_url": "https://chatgpt.com.evil.test/backend-api/codex"},
    {"base_url": "http://chatgpt.com/backend-api/codex"},
    {"api_key": ""}, {"api_mode": "chat_completions"},
])
def test_codex_named_route_refuses_other_billing_or_missing_auth(changes):
    from types import SimpleNamespace
    from tools.custom_subagents import resolve_named_credentials
    settings = dict(provider="openai-codex", model="gpt-6-astra", base_url="https://chatgpt.com/backend-api/codex", api_mode="codex_responses", api_key="fixture-token", request_overrides={})
    parent = SimpleNamespace(**{**settings, **changes})
    selected = parse_definitions({"subagents": {"worker": definition()}})["worker"]
    with pytest.raises(ValueError, match="subscription route"):
        resolve_named_credentials(selected, {}, parent)


def test_unsupported_explicit_effort_never_clamps():
    from types import SimpleNamespace
    from tools.custom_subagents import resolve_named_credentials
    parent = SimpleNamespace(provider="openai-codex", model="gpt-6-astra", base_url="https://chatgpt.com/backend-api/codex", api_mode="codex_responses", api_key="fixture-token", request_overrides={})
    selected = parse_definitions({"subagents": {"worker": definition(reasoning_effort="minimal")}})["worker"]
    with pytest.raises(ValueError, match="unsupported"):
        resolve_named_credentials(selected, {}, parent)


def test_invalid_batch_never_constructs_valid_sibling(monkeypatch):
    import json
    from types import SimpleNamespace
    from tools import delegate_tool
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"subagents": {"worker": definition()}})
    parent = SimpleNamespace(provider="openai-codex", model="gpt-6-astra", base_url="https://chatgpt.com/backend-api/codex", api_mode="codex_responses", api_key="fixture-token", request_overrides={})
    def unexpected(**kwargs):
        pytest.fail("Preflight-invalid batch constructed a child")
    monkeypatch.setattr(delegate_tool, "_build_child_preserving_parent_tools", unexpected)
    result = json.loads(delegate_tool.delegate_task(tasks=[
        {"goal": "Implement a scoped fixture", "subagent_type": "worker"},
        {"goal": "Investigate a scoped fixture", "subagent_type": "unknown"},
    ], parent_agent=parent))
    assert "Task 1" in result["error"]
    assert "Unknown subagent_type" in result["error"]


@pytest.mark.parametrize("defaults,parent_effort,expected", [
    ({}, {"effort": "high"}, "high"),
    ({"reasoning_effort": "low"}, {"effort": "high"}, "low"),
    ({}, None, "medium"),
    ({}, {"effort": "ultra"}, "max"),
])
def test_codex_omitted_effort_inherits_without_changing_route(defaults, parent_effort, expected):
    from types import SimpleNamespace
    from tools.custom_subagents import resolve_named_credentials
    parent = SimpleNamespace(provider="openai-codex", model="gpt-6-astra",
        base_url="https://chatgpt.com/backend-api/codex", api_mode="codex_responses",
        api_key="fixture-token", reasoning_config=parent_effort, request_overrides={})
    raw = definition()
    del raw["reasoning_effort"]
    selected = parse_definitions({"subagents": {"worker": raw}})["worker"]
    creds, reasoning = resolve_named_credentials(selected, defaults, parent)
    assert reasoning == {"enabled": True, "effort": expected}
    assert creds["provider"] == "openai-codex"
    assert creds["api_key"] == parent.api_key
    assert parent.reasoning_config == parent_effort


@pytest.mark.parametrize("provider,explicit,expected", [
    ("openrouter", None, None),
    ("custom", None, None),
])
def test_non_codex_inheritance_uses_real_resolver_and_profile(provider, explicit, expected):
    from types import SimpleNamespace
    from tools.custom_subagents import resolve_named_credentials
    parent = SimpleNamespace(provider=provider, model="fixture-model",
        reasoning_config={"effort": "high"}, request_overrides={})
    raw = {"description": "Fixture", "instructions": "Fixture", "model": "fixture-model"}
    if explicit:
        raw["reasoning_effort"] = explicit
    selected = parse_definitions({"subagents": {"worker": raw}})["worker"]
    creds, reasoning = resolve_named_credentials(selected, {}, parent)
    assert creds["model"] == "fixture-model"
    assert creds["provider"] is None  # Native resolver's parent-inheritance marker.
    assert reasoning == expected


@pytest.mark.parametrize("explicit,supported,expected", [
    ("medium", ("low", "medium", "high"), "medium"),
    (None, ("low", "medium"), "medium"),
    (None, (), None),
    ("medium", (), "error"),
    ("medium", None, "error"),
])
def test_explicit_non_codex_provider_effort_contract(monkeypatch, explicit, supported, expected):
    from types import SimpleNamespace
    import providers
    from hermes_cli import runtime_provider
    from tools.custom_subagents import resolve_named_credentials
    calls = []
    def runtime(**kwargs):
        calls.append(kwargs)
        return {"provider": "fixture-provider", "api_key": "fixture-token",
            "base_url": "https://fixture.invalid/v1", "api_mode": "chat_completions"}
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", runtime)
    monkeypatch.setattr(providers, "get_provider_profile", lambda provider:
        SimpleNamespace(supported_reasoning_efforts=lambda model: supported))
    raw = definition(provider="fixture-provider", model="fixture-model")
    if explicit is None:
        del raw["reasoning_effort"]
    else:
        raw["reasoning_effort"] = explicit
    selected = parse_definitions({"subagents": {"worker": raw}})["worker"]
    parent = SimpleNamespace(provider="openai-codex", reasoning_config={"effort": "high"})
    defaults = {"provider": "unrelated", "api_key": "wrong-fixture",
        "base_url": "https://wrong.invalid", "api_mode": "codex_responses"}
    if expected == "error":
        with pytest.raises(ValueError, match="unsupported"):
            resolve_named_credentials(selected, defaults, parent)
    else:
        creds, reasoning = resolve_named_credentials(selected, defaults, parent)
        assert creds["api_key"] == "fixture-token"
        assert creds["provider"] == "fixture-provider"
        assert reasoning == ({"enabled": True, "effort": expected} if expected else None)
    assert calls == [{"requested": "fixture-provider", "target_model": "fixture-model"}]
    assert defaults["api_key"] == "wrong-fixture"


def test_empty_internal_credentials_retains_legacy_config(monkeypatch):
    import json
    from types import SimpleNamespace
    from tools import delegate_tool
    cfg = {"provider": "fixture-provider", "model": "fixture-model"}
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    seen = []
    def observe(config, parent):
        seen.append(config)
        raise ValueError("fixture stops before child construction")
    monkeypatch.setattr(delegate_tool, "_resolve_delegation_credentials", observe)
    result = json.loads(delegate_tool.delegate_task(tasks=[{"goal": "Fixture"}],
        parent_agent=SimpleNamespace(), credentials_cfg={}))
    assert "fixture stops" in result["error"]
    assert seen == [cfg]


def test_discovery_exposes_only_selection_guidance(monkeypatch):
    from tools import delegate_tool
    raw = definition()
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"subagents": {"worker": raw}})
    schema = delegate_tool._build_dynamic_schema_overrides()
    field = schema["parameters"]["properties"]["tasks"]["items"]["properties"]["subagent_type"]
    assert field["enum"] == ["worker"]
    assert raw["description"] in field["description"]
    assert raw["instructions"] not in str(schema)
    assert "subagent_type" not in delegate_tool.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]
