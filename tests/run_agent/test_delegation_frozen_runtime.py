"""Frozen custom-subagent fallback, MoA, and resume contracts."""
import json
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest


def test_inherited_role_freezes_ordered_fallbacks(monkeypatch):
    import providers
    from hermes_cli import runtime_provider
    from tools.custom_subagents import freeze_fallback_routes, parse_definitions
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model, "base_url": f"https://{requested}.fixture/v1",
        "api_key": "fixture-secret", "api_mode": "codex_responses" if requested == "openai-codex" else "chat_completions"})
    monkeypatch.setattr(providers, "get_provider_profile", lambda _provider: SimpleNamespace(
        supported_reasoning_efforts=lambda _model: {"high"}))
    role = parse_definitions({"subagents": {"lead": {"description": "Lead", "instructions": "Own it",
        "inherit_parent": True, "fallbacks": [{"provider": "openai-codex", "model": "astra-fixture",
        "reasoning_effort": "high"}]}}})["lead"]
    routes = freeze_fallback_routes(role, primary_provider="anthropic", primary_model="fable-fixture")
    assert [(r.provider, r.model, r.api_mode) for r in routes] == [("openai-codex", "astra-fixture", "codex_responses")]
    assert "fixture-secret" not in repr(routes)


def test_two_moa_presets_freeze_physical_routes_privacy_and_secrets(monkeypatch):
    from agent import moa_loop
    from hermes_cli import runtime_provider
    presets = {"one": {"reference_models": [{"provider": "p1", "model": "r1", "api_key": "raw-secret"}],
                       "aggregator": {"provider": "p2", "model": "a1"}},
               "two": {"reference_models": [{"provider": "p3", "model": "r2"}],
                       "aggregator": {"provider": "p4", "model": "a2"}}}
    monkeypatch.setattr(moa_loop, "_resolve_preset_cached", lambda name: (presets[name], {"privacy_filter": "full"}))
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model, "base_url": f"https://{requested}.fixture/v1",
        "api_key": f"secret-{requested}", "api_mode": "chat_completions"})
    one, two = moa_loop.snapshot_moa_preset("one"), moa_loop.snapshot_moa_preset("two")
    assert one.fingerprint != two.fingerprint and one.options == {"privacy_filter": "full"}
    presets["one"]["reference_models"][0]["model"] = "drift"
    assert one.preset["reference_models"][0]["model"] == "r1"
    assert "secret" not in json.dumps(one.metadata())



def test_frozen_route_metadata_preserves_token_limits_without_raw_secrets(monkeypatch):
    from agent import moa_loop
    from tools.custom_subagents import ResolvedRoute

    preset = {"reference_models": [{"provider": "p1", "model": "r1", "max_tokens": 321}],
              "aggregator": {"provider": "pa", "model": "a1"}, "reference_max_tokens": 654}
    monkeypatch.setattr(moa_loop, "_resolve_preset_cached", lambda _name: (preset, {}))
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model, "base_url": f"https://{requested}.fixture/v1",
        "api_key": f"runtime-secret-{requested}", "api_mode": "chat_completions",
        "request_overrides": {"max_output_tokens": 987, "max_tokens": "limit-secret",
                              "access_token": "override-secret"}})
    metadata = moa_loop.snapshot_moa_preset("limits").metadata()
    public = metadata["preset_snapshot"]
    assert public["reference_max_tokens"] == 654
    assert public["reference_models"][0]["max_tokens"] == 321
    assert public["reference_models"][0]["runtime_identity"]["request_overrides"] == {"max_output_tokens": 987}
    restored = moa_loop.restore_moa_preset(metadata)
    assert restored.preset["reference_max_tokens"] == 654
    assert restored.preset["reference_models"][0]["max_tokens"] == 321

    route = ResolvedRoute("p1", "r1", "https://p1.fixture/v1", "chat_completions", None,
        "route-secret", hashlib.sha256(b"route-secret").hexdigest(),
        json.dumps({"max_completion_tokens": 123, "access_token": "route-override-secret"}))
    assert route.metadata()["request_overrides"] == {"max_completion_tokens": 123}
    durable = json.dumps({"moa": metadata, "route": route.metadata()})
    assert all(secret not in durable for secret in ("runtime-secret", "override-secret", "route-secret", "limit-secret"))


def test_frozen_fallback_allows_authorized_same_route_pool_rotation():
    from agent.chat_completion_helpers import _rebind_fallback_credential_pool
    from tools.custom_subagents import ResolvedRoute, RuntimePin

    old_key, rotated_key = "fallback-account-a", "fallback-account-b"
    rotated_entry = SimpleNamespace(provider="fallback-provider", runtime_api_key=rotated_key,
                                    runtime_base_url="https://fallback.fixture/v1")
    fallback_pool: Any = SimpleNamespace(provider="fallback-provider")
    fallback_pool.entries = lambda: [rotated_entry]
    primary_pool: Any = SimpleNamespace(provider="primary-provider", entries=lambda: [])
    route = ResolvedRoute("fallback-provider", "fallback-model", "https://fallback.fixture/v1",
        "chat_completions", None, old_key, hashlib.sha256(old_key.encode()).hexdigest(), "{}",
        _credential_pool=fallback_pool)
    pin = RuntimePin("fixture", "primary-provider", "primary-model", "https://primary.fixture/v1",
        "chat_completions", None, hashlib.sha256(b"primary-key").hexdigest(), True,
        primary_pool, (route,), "{}")
    child = SimpleNamespace(provider=route.provider, model=route.model, base_url=route.base_url,
        api_mode=route.api_mode, api_key=old_key, request_overrides={},
        _credential_pool=primary_pool, _credential_pool_entry_id="primary-account")
    _rebind_fallback_credential_pool(child, route.provider, route.model,
                                     frozen_pool=route._credential_pool)
    assert child._credential_pool is fallback_pool and child._credential_pool_entry_id is None

    rotated_pin = pin.for_pool_swap(child, rotated_entry, rotated_key, rotated_entry.runtime_base_url)
    child.api_key = rotated_key
    rotated_pin.validate_request(child, {"model": route.model},
                                 client=SimpleNamespace(api_key=rotated_key, base_url=route.base_url))
    active = rotated_pin.fallback_routes[0]
    assert active.credential_digest == hashlib.sha256(rotated_key.encode()).hexdigest()
    assert (active.provider, active.model, active.base_url, active.api_mode) == (
        route.provider, route.model, route.base_url, route.api_mode)
    assert rotated_key not in repr(rotated_pin) and rotated_key not in json.dumps(active.metadata())

    child._credential_pool = SimpleNamespace(provider="fallback-provider", entries=lambda: [rotated_entry])
    with pytest.raises(ValueError, match="unauthorized credential rotation"):
        pin.for_pool_swap(child, rotated_entry, rotated_key, route.base_url)


def _resume_fixture(monkeypatch):
    from tools.custom_subagents import parse_definitions
    from hermes_cli import runtime_provider
    cfg = {"subagents": {"advisor": {"description": "Advise", "instructions": "Analyze",
        "provider": "fixture", "model": "m", "reasoning_effort": "high"}}}
    metadata = {"version": 1, "subagent_type": "advisor", "description": "Advise", "instructions": "Analyze",
        "parent_session_root": "root", "provider": "fixture", "model": "m", "base_url": "https://fixture/v1",
        "api_mode": "chat_completions", "reasoning_effort": "high",
        "authority_fingerprint": hashlib.sha256(b"secret").hexdigest(),
        "request_overrides": {}, "fallbacks": [], "enabled_toolsets": ["web"]}
    class DB:
        def resolve_resume_session_id(self, sid): return "tip" if sid == "child" else sid
        def get_session(self, _sid): return {"profile_name": "default", "model_config": json.dumps({
            "_delegation_launch": metadata, "_delegation_completed": True, "_delegate_from": "root"})}
        def get_compression_lineage(self, sid): return ["root"] if sid == "root" else ["root", sid]
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **_k: {
        "provider": "fixture", "model": "m", "base_url": "https://fixture/v1",
        "api_key": "secret", "api_mode": "chat_completions"})
    return metadata, parse_definitions(cfg), type("P", (), {"session_id": "parent", "_session_db": DB()})()


def test_primary_launch_metadata_redacts_secrets_but_keeps_runtime_pin_and_resumes(monkeypatch):
    """Durable primary metadata is public; the in-memory pin remains exact."""
    from tools import delegate_tool
    from tools.custom_subagents import parse_definitions

    raw_overrides = {
        "max_output_tokens": 321,
        "authorization": "PRIMARY-AUTH-SENTINEL",
        "nested": {"access_token": "PRIMARY-NESTED-SENTINEL", "safe": "value"},
        "extra_headers": {"X-Api-Key": "PRIMARY-HEADER-SENTINEL"},
    }
    definition = parse_definitions({"subagents": {"advisor": {
        "description": "Advise", "instructions": "Analyze", "provider": "fixture", "model": "m",
    }}})["advisor"]

    class Child:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self._session_init_model_config = {}

    parent = SimpleNamespace(
        session_id="root", api_key="PRIMARY-API-SENTINEL", prefill_messages=None,
        _delegate_depth=0, _fallback_chain=[], request_overrides={}, model="parent-model",
    )
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(delegate_tool, "_resolve_child_runtime", lambda *_a, **_k: {
        "provider": "fixture", "model": "m", "base_url": "https://fixture/v1",
        "api_key": "PRIMARY-API-SENTINEL", "api_mode": "chat_completions",
        "fallback_model": None,
    })
    monkeypatch.setattr(delegate_tool, "_resolve_child_toolsets", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(delegate_tool, "_open_child_session_db", lambda _parent: None)
    monkeypatch.setattr(delegate_tool, "_attach_child", lambda *_a: None)
    monkeypatch.setattr("run_agent.AIAgent", Child)

    child = delegate_tool._build_child_agent(
        0, "analyze", None, None, "m", 1, 1, parent, override_provider="fixture",
        override_base_url="https://fixture/v1", override_api_key="PRIMARY-API-SENTINEL",
        override_api_mode="chat_completions", override_request_overrides=raw_overrides,
        subagent_definition=definition,
    )
    launch = child._delegation_launch_metadata
    durable = json.dumps(launch)
    assert launch["request_overrides"] == {"max_output_tokens": 321, "nested": {"safe": "value"}}
    assert all(sentinel not in durable for sentinel in (
        "PRIMARY-API-SENTINEL", "PRIMARY-AUTH-SENTINEL", "PRIMARY-NESTED-SENTINEL",
        "PRIMARY-HEADER-SENTINEL",
    ))

    pin = child._delegation_runtime_pin
    assert "PRIMARY-AUTH-SENTINEL" in pin.request_overrides_json
    pin.validate_request(child, {"model": "m"}, client=SimpleNamespace(
        api_key="PRIMARY-API-SENTINEL", base_url="https://fixture/v1",
    ))
    child.request_overrides = {"max_output_tokens": 321}
    with pytest.raises(ValueError, match="pinned request overrides changed"):
        pin.validate_request(child, {"model": "m"}, client=SimpleNamespace(
            api_key="PRIMARY-API-SENTINEL", base_url="https://fixture/v1",
        ))

    child.request_overrides = raw_overrides
    class DB:
        def resolve_resume_session_id(self, _sid): return "child"
        def get_session(self, _sid): return {"profile_name": "default", "model_config": json.dumps({
            "_delegation_launch": launch, "_delegation_completed": True, "_delegate_from": "root",
        })}
        def get_compression_lineage(self, _sid): return ["root"]
    resume_parent = SimpleNamespace(session_id="root", _session_db=DB())
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **_k: {
        "provider": "fixture", "model": "m", "base_url": "https://fixture/v1",
        "api_key": "PRIMARY-API-SENTINEL", "api_mode": "chat_completions",
        "request_overrides": raw_overrides,
    })
    resumed = delegate_tool._resolve_resume_launch({"resume_session_id": "child"}, {"advisor": definition}, resume_parent)
    assert resumed.credentials["request_overrides"] == raw_overrides


def test_real_constructor_named_moa_child_has_no_parent_fallback_chain(monkeypatch):
    """An explicitly pinned named MoA child cannot inherit its parent's fallbacks."""
    from tools import delegate_tool
    from tools.custom_subagents import parse_definitions

    definition = parse_definitions({"subagents": {"council": {
        "description": "Review", "instructions": "Review", "provider": "moa", "model": "review",
    }}})["council"]
    parent = SimpleNamespace(
        session_id="root", api_key=None, prefill_messages=None, _delegate_depth=0,
        _fallback_chain=[{"provider": "outer", "model": "fallback"}], request_overrides={},
        model="parent-model", provider="fixture", api_mode="chat_completions", base_url="https://fixture/v1",
    )
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(delegate_tool, "_resolve_child_runtime", lambda *_a, **_k: {
        "provider": "moa", "model": "review", "base_url": "moa://local", "api_key": None,
        "api_mode": "chat_completions", "fallback_model": None,
    })
    monkeypatch.setattr(delegate_tool, "_resolve_child_toolsets", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(delegate_tool, "_open_child_session_db", lambda _parent: None)
    monkeypatch.setattr(delegate_tool, "_attach_child", lambda *_a: None)

    child = delegate_tool._build_child_agent(
        0, "review", None, None, "review", 1, 1, parent, override_provider="moa",
        override_base_url="moa://local", override_api_mode="chat_completions",
        subagent_definition=definition, moa_snapshot=SimpleNamespace(metadata=lambda: {"preset": "review"}),
    )
    assert child._fallback_chain == [] and child._fallback_index == 0
    assert not (child._fallback_index < len(child._fallback_chain))


def test_resume_restores_compression_tip_and_rejects_parent_sibling(monkeypatch):
    from tools import delegate_tool
    metadata, definitions, parent = _resume_fixture(monkeypatch)
    launch = delegate_tool._resolve_resume_launch({"resume_session_id": "child"}, definitions, parent)
    assert launch.resume_session_id == "tip" and launch.enabled_toolsets == ("web",)
    metadata["parent_session_root"] = "sibling-root"
    with pytest.raises(ValueError, match="foreign"):
        delegate_tool._resolve_resume_launch({"resume_session_id": "child"}, definitions, parent)


def test_resume_preserves_launch_metadata_and_uses_stable_pool_identity(monkeypatch):
    from tools import delegate_tool

    metadata, definitions, original_parent = _resume_fixture(monkeypatch)
    entry = SimpleNamespace(id="account-a", provider="fixture", runtime_api_key="refreshed-secret",
                            runtime_base_url="https://fixture/v1")
    pool = SimpleNamespace(entries=lambda: [entry])
    parent = SimpleNamespace(session_id="parent", _session_db=original_parent._session_db,
                             _credential_pool=pool)
    metadata["credential_pool_entry_id"] = "account-a"
    monkeypatch.setattr(delegate_tool, "_resolve_child_credential_pool", lambda *_a, **_k: pool)
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **_k: {
        "provider": "fixture", "model": "m", "base_url": "https://fixture/v1",
        "api_key": "different-current-secret", "api_mode": "chat_completions"})

    launch = delegate_tool._resolve_resume_launch({"resume_session_id": "child"}, definitions, parent)
    assert launch.credentials["api_key"] == "refreshed-secret"
    assert launch.launch_metadata == metadata
    assert "refreshed-secret" not in json.dumps(launch.launch_metadata)

    metadata["credential_pool_entry_id"] = "unknown-account"
    with pytest.raises(ValueError, match="stable credential identity"):
        delegate_tool._resolve_resume_launch({"resume_session_id": "child"}, definitions, parent)


def test_resumed_launch_metadata_is_seeded_for_next_compression():
    from tools import delegate_tool

    metadata = {"version": 1, "subagent_type": "advisor"}
    child = SimpleNamespace(_session_init_model_config={})
    delegate_tool._seed_resumed_launch_metadata(child, metadata)
    assert child._session_init_model_config["_delegation_launch"] == metadata
    assert child._delegation_launch_metadata == metadata


def test_resumed_moa_uses_fresh_virtual_route_shape(monkeypatch):
    from agent import moa_loop
    from tools import delegate_tool
    from tools.custom_subagents import parse_definitions

    snapshot = SimpleNamespace(metadata=lambda: {"preset": "review"})
    metadata = {
        "version": 1, "subagent_type": "council", "description": "Review", "instructions": "Review",
        "parent_session_root": "root", "provider": "moa", "model": "review", "base_url": "moa://local",
        "api_mode": "chat_completions", "reasoning_effort": None, "authority_fingerprint": None,
        "request_overrides": {}, "fallbacks": [], "enabled_toolsets": [], "moa": {"preset": "review"},
    }
    class DB:
        def resolve_resume_session_id(self, _sid): return "tip"
        def get_session(self, _sid): return {"profile_name": "default", "model_config": json.dumps({
            "_delegation_launch": metadata, "_delegation_completed": True, "_delegate_from": "root"})}
        def get_compression_lineage(self, sid): return ["root"] if sid == "root" else ["root", sid]

    parent = SimpleNamespace(session_id="parent", _session_db=DB())
    definitions = parse_definitions({"subagents": {"council": {
        "description": "Review", "instructions": "Review", "provider": "moa", "model": "review"}}})
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr(moa_loop, "restore_moa_preset", lambda _metadata: snapshot)

    launch = delegate_tool._resolve_resume_launch({"resume_session_id": "child"}, definitions, parent)
    assert launch.credentials == {
        "provider": "moa", "model": "review", "base_url": "moa://local", "api_key": None,
        "api_mode": "chat_completions", "request_overrides": None, "max_output_tokens": None,
    }


def test_resume_rejects_role_conflict_and_schema_exposes_handle(monkeypatch):
    from tools import delegate_tool
    _metadata, definitions, parent = _resume_fixture(monkeypatch)
    with pytest.raises(ValueError, match="conflicts"):
        delegate_tool._resolve_resume_launch({"resume_session_id": "child", "subagent_type": "worker"}, definitions, parent)
    props = delegate_tool.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert "resume_session_id" in props


def test_resume_rejects_control_only_subagent_id_before_database_lookup():
    from tools import delegate_tool

    db = SimpleNamespace(resolve_resume_session_id=lambda _sid: pytest.fail("control id reached database"))
    parent = SimpleNamespace(_session_db=db)
    with pytest.raises(ValueError, match="durable child_session_id.*control-only subagent_id"):
        delegate_tool._resolve_resume_launch({"resume_session_id": "sa-0-deadbeef"}, {}, parent)


def test_delegation_schema_uses_safe_new_task_label_and_distinguishes_ids():
    from tools.delegate_tool import DELEGATE_TASK_SCHEMA

    props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]
    assert set(props["required"]) == {"goal"}
    assert "never use the goal" in props["properties"]["task_label"]["description"].lower()
    assert "control-only subagent_id" in props["properties"]["resume_session_id"]["description"]


def test_named_launch_metadata_path_accepts_resolved_launch_object():
    from tools.delegate_tool import _task_routing_metadata
    from tools.custom_subagents import ResolvedSubagentLaunch, SubagentDefinition
    definition = SubagentDefinition("advisor", "Advise", "Analyze", "anthropic", "fable", "high")
    launch = ResolvedSubagentLaunch(definition, {"provider": "anthropic", "model": "fable"},
                                    {"enabled": True, "effort": "high"})
    assert _task_routing_metadata([launch])[0] == {
        "subagent_type": "advisor", "provider": "anthropic", "model": "fable",
        "reasoning_effort": "high",
    }


def test_frozen_moa_two_presets_dispatch_every_physical_route(monkeypatch):
    from agent import moa_loop

    registries = {
        "alpha": {"reference_models": [
            {"provider": "p1", "model": "r1"},
            {"provider": "p2", "model": "r2"},
        ], "aggregator": {"provider": "pa", "model": "a1"}},
        "beta": {"reference_models": [
            {"provider": "p3", "model": "r3"},
        ], "aggregator": {"provider": "pb", "model": "a2"}},
    }
    monkeypatch.setattr(moa_loop, "_resolve_preset_cached", lambda name: (
        registries[name], {"privacy_filter": "full"}
    ))
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model,
        "base_url": f"https://{requested}.fixture/v1", "api_key": f"key-{requested}",
        "api_mode": "chat_completions",
    })
    snapshots = [moa_loop.snapshot_moa_preset(name) for name in ("alpha", "beta")]
    registries["alpha"]["reference_models"][0]["model"] = "registry-drift"
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(content="advice" if kwargs["task"] == "moa_reference" else "answer",
                                  tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                               usage=None, model=kwargs["model"])

    monkeypatch.setattr(moa_loop, "call_llm", fake_call_llm)
    for snapshot in snapshots:
        owner = SimpleNamespace(_moa_preset_snapshot=snapshot, _interrupt_requested=False)
        facade = moa_loop.MoAChatCompletions(snapshot.name, agent=owner)
        facade.create(messages=[{"role": "user", "content": "review"}], tools=[])

    routes = [(c["task"], c["provider"], c["model"], c["api_mode"]) for c in calls]
    assert sorted(routes[:2]) == sorted([
        ("moa_reference", "p1", "r1", "chat_completions"),
        ("moa_reference", "p2", "r2", "chat_completions"),
    ])
    assert routes[2:] == [
        ("moa_aggregator", "pa", "a1", "chat_completions"),
        ("moa_reference", "p3", "r3", "chat_completions"),
        ("moa_aggregator", "pb", "a2", "chat_completions"),
    ]
    assert all(call["strict_route"] is True for call in calls)


def test_frozen_moa_token_limit_reaches_real_auxiliary_dispatch(monkeypatch):
    from agent import auxiliary_client, moa_loop

    preset = {
        "reference_models": [{"provider": "openrouter", "model": "gpt-5.1"}],
        "aggregator": {"provider": "openrouter", "model": "gpt-5.1"},
    }
    monkeypatch.setattr(moa_loop, "_resolve_preset_cached", lambda _name: (preset, {}))
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model,
        "base_url": "https://fixture.invalid/v1", "api_key": "fixture-key",
        "api_mode": "chat_completions",
        "request_overrides": {"max_output_tokens": 987},
    })
    physical_calls = []

    class Completions:
        def create(self, **kwargs):
            physical_calls.append(kwargs)
            message = SimpleNamespace(content="advice", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                                   usage=None, model=kwargs["model"])

    client = SimpleNamespace(base_url="https://fixture.invalid/v1", api_key="fixture-key",
                             chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(auxiliary_client, "_get_cached_client", lambda *_a, **_k: (client, "gpt-5.1"))
    monkeypatch.setattr(auxiliary_client, "_effective_provider_for_client", lambda *_a, **_k: "openrouter")
    monkeypatch.setattr(auxiliary_client, "_validate_llm_response", lambda response, *_a, **_k: response)

    snapshot = moa_loop.snapshot_moa_preset("token-capped")
    owner = SimpleNamespace(_moa_preset_snapshot=snapshot, _interrupt_requested=False)
    moa_loop.MoAChatCompletions(snapshot.name, agent=owner).create(
        messages=[{"role": "user", "content": "review"}], tools=[])

    assert len(physical_calls) == 2
    assert all(call["max_completion_tokens"] == 987 for call in physical_calls)
    assert all("max_output_tokens" not in call for call in physical_calls)


def test_ordinary_moa_keeps_native_auxiliary_recovery_ladder(monkeypatch):
    from agent import moa_loop

    preset = {"reference_models": [{"provider": "p1", "model": "r1"}],
              "aggregator": {"provider": "pa", "model": "a1"}}
    monkeypatch.setattr(moa_loop, "_resolve_preset_cached", lambda _name: (preset, {}))
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model,
        "base_url": f"https://{requested}.fixture/v1", "api_key": f"key-{requested}",
        "api_mode": "chat_completions"})
    calls = []
    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(content="advice" if kwargs["task"] == "moa_reference" else "answer",
                                  tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                               usage=None, model=kwargs["model"])
    monkeypatch.setattr(moa_loop, "call_llm", fake_call_llm)
    moa_loop.MoAChatCompletions("ordinary", agent=SimpleNamespace(_interrupt_requested=False)).create(
        messages=[{"role": "user", "content": "review"}], tools=[])
    assert calls and all(call["strict_route"] is False for call in calls)


def test_moa_resume_rejects_credential_authority_rotation(monkeypatch):
    from agent import moa_loop
    keys = {"p1": "first", "pa": "aggregator"}
    preset = {"reference_models": [{"provider": "p1", "model": "r1"}],
              "aggregator": {"provider": "pa", "model": "a1"}}
    monkeypatch.setattr(moa_loop, "_resolve_preset_cached", lambda _name: (preset, {}))
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model,
        "base_url": f"https://{requested}.fixture/v1", "api_key": keys[requested],
        "api_mode": "chat_completions",
    })
    metadata = moa_loop.snapshot_moa_preset("review").metadata()
    keys["p1"] = "rotated"
    with pytest.raises(ValueError, match="frozen authority"):
        moa_loop.restore_moa_preset(metadata)


def test_real_session_db_claims_completed_resume_once(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", source="tool", model_config={"_delegation_completed": True})
    assert db.claim_delegated_resume("child") is True
    assert db.claim_delegated_resume("child") is False
    config = json.loads(db.get_session("child")["model_config"])
    assert config["_delegation_completed"] is False
    assert config["_delegation_resume_claimed_at"] > 0


def test_real_session_db_batch_claim_is_all_or_none(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    for session_id in ("first", "second", "third"):
        db.create_session(
            session_id, source="tool",
            model_config={"_delegation_completed": True},
        )

    assert db.claim_delegated_resume("second") is True
    assert db.claim_delegated_resumes(["first", "second"]) is False
    first_row = db.get_session("first")
    assert first_row is not None
    first = json.loads(first_row["model_config"])
    assert first["_delegation_completed"] is True
    assert "_delegation_resume_claimed_at" not in first

    assert db.claim_delegated_resumes(["first", "third"]) is True
    for session_id in ("first", "third"):
        row = db.get_session(session_id)
        assert row is not None
        config = json.loads(row["model_config"])
        assert config["_delegation_completed"] is False
        assert config["_delegation_resume_claimed_at"] > 0


def test_resume_claim_release_is_exact_and_retryable(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", source="tool", model_config={"_delegation_completed": True})
    assert db.claim_delegated_resumes(["child"], claim_id="claim-a") is True
    assert db.release_delegated_resumes(["child"], claim_id="wrong") is False
    assert db.release_delegated_resumes(["child"], claim_id="claim-a") is True
    config = json.loads(db.get_session("child")["model_config"])
    assert config["_delegation_completed"] is True
    assert "_delegation_resume_claimed_at" not in config
    assert db.claim_delegated_resume("child") is True


def test_build_failure_restores_consumed_resume_grant(monkeypatch, tmp_path):
    from hermes_state import SessionDB
    from tools import delegate_tool
    from tools.custom_subagents import ResolvedSubagentLaunch

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", source="tool", model_config={"_delegation_completed": True})
    assert db.claim_delegated_resumes(["child"], claim_id="claim-a")
    creds = {"provider": "fixture", "model": "m", "base_url": "https://fixture/v1",
             "api_key": "secret", "api_mode": "chat_completions"}
    launch = ResolvedSubagentLaunch(None, creds, None, resume_session_id="child", resume_claim_id="claim-a")
    monkeypatch.setattr(delegate_tool, "_build_child_preserving_parent_tools",
                        lambda **_kwargs: (_ for _ in ()).throw(ValueError("build failed")))
    children, error = delegate_tool._build_children(
        [{"goal": "continue"}], [None], creds, top_role="worker", max_iterations=250,
        parent_agent=SimpleNamespace(_session_db=db), live_deleg_id=None, live_writers=[],
        task_runtime=[launch],
    )
    assert children == [] and error == "build failed"
    config = json.loads(db.get_session("child")["model_config"])
    assert config["_delegation_completed"] is True
    assert "_delegation_resume_claimed_at" not in config


def test_unadmitted_lease_failure_restores_consumed_resume_grant(tmp_path):
    from hermes_state import SessionDB
    from tools.delegate_tool import _restore_unadmitted_resume_grant

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", source="tool", model_config={"_delegation_completed": True})
    assert db.claim_delegated_resumes(["child"], claim_id="claim-a")
    child = SimpleNamespace(
        _session_db=db, session_id="child", _delegation_resume_claim_id="claim-a",
        _delegation_resume_admitted=False,
    )
    assert _restore_unadmitted_resume_grant(child) is True
    assert db.claim_delegated_resume("child") is True
    child._delegation_resume_admitted = True
    assert _restore_unadmitted_resume_grant(child) is False
    assert json.loads(db.get_session("child")["model_config"])["_delegation_completed"] is False


def test_resume_preflight_claims_only_after_whole_batch_validates(monkeypatch):
    from tools import delegate_tool
    from tools.custom_subagents import ResolvedSubagentLaunch

    claims = []

    class DB:
        def claim_delegated_resumes(self, session_ids, **_kwargs):
            claims.append(list(session_ids))
            return True

    parent = SimpleNamespace(_session_db=DB())

    def resolve(task, _definitions, _parent):
        if task["resume_session_id"] == "invalid":
            raise ValueError("invalid resume fixture")
        return ResolvedSubagentLaunch(
            None, {}, None, resume_session_id=task["resume_session_id"]
        )

    monkeypatch.setattr(delegate_tool, "_resolve_resume_launch", resolve)
    tasks = [
        {"resume_session_id": "first"},
        {"resume_session_id": "invalid"},
    ]
    launches, error = delegate_tool._preflight_task_runtime(
        tasks, {}, None, parent, {},
    )
    assert launches == [] and "Task 1 preflight failed" in error
    assert claims == []

    tasks[1]["resume_session_id"] = "second"
    launches, error = delegate_tool._preflight_task_runtime(
        tasks, {}, None, parent, {},
    )
    assert error is None
    assert [launch.resume_session_id for launch in launches] == ["first", "second"]
    assert claims == [["first", "second"]]


def test_strict_auxiliary_route_never_enters_provider_fallback_ladder(monkeypatch):
    from agent import auxiliary_client

    class Completions:
        def create(self, **_kwargs):
            raise RuntimeError("fixture transport unavailable")

    client = SimpleNamespace(
        base_url="https://strict.fixture/v1", api_key="strict-key",
        chat=SimpleNamespace(completions=Completions()),
    )
    monkeypatch.setattr(auxiliary_client, "_get_cached_client", lambda *_a, **_k: (client, "strict-model"))
    monkeypatch.setattr(auxiliary_client, "_effective_provider_for_client", lambda *_a, **_k: "strict")
    monkeypatch.setattr(
        auxiliary_client, "_start_recovery_ladder",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("fallback ladder entered")),
    )
    with pytest.raises(RuntimeError, match="fixture transport unavailable"):
        auxiliary_client._call_llm_impl(
            task="", provider="strict", model="strict-model",
            base_url="https://strict.fixture/v1", api_key="strict-key",
            api_mode="chat_completions", messages=[{"role": "user", "content": "x"}],
            strict_route=True,
        )

def test_resumed_child_leases_the_authorized_stable_credential():
    from tools.delegate_tool_child_run import _lease_child_credential

    selected = []
    entry = SimpleNamespace(id="account-b")
    pool = SimpleNamespace(
        acquire_lease=lambda credential_id: selected.append(credential_id) or credential_id,
        current=lambda: entry,
    )
    swapped = []
    child = SimpleNamespace(
        _credential_pool=pool, _delegation_resume_credential_id="account-b",
        _swap_credential=lambda value: swapped.append(value),
    )
    assert _lease_child_credential(child) == (pool, "account-b")
    assert selected == ["account-b"] and swapped == [entry]


def test_global_delegation_segment_budget_remains_250():
    from tools.delegate_tool import DEFAULT_MAX_ITERATIONS
    assert DEFAULT_MAX_ITERATIONS == 250


def test_resume_rejects_changed_authentication_header_override(monkeypatch):
    from tools import delegate_tool
    from tools.custom_subagents import _authority_mapping_fingerprint

    metadata, definitions, parent = _resume_fixture(monkeypatch)
    original = {"max_output_tokens": 321, "extra_headers": {"X-Api-Key": "first-secret"}}
    changed = {"max_output_tokens": 321, "extra_headers": {"X-Api-Key": "second-secret"}}
    metadata["request_overrides"] = {"max_output_tokens": 321}
    metadata["request_overrides_fingerprint"] = _authority_mapping_fingerprint(original)
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **_k: {
        "provider": "fixture", "model": "m", "base_url": "https://fixture/v1",
        "api_key": "secret", "api_mode": "chat_completions", "request_overrides": changed,
    })

    with pytest.raises(ValueError, match="primary route can no longer be authorized exactly"):
        delegate_tool._resolve_resume_launch({"resume_session_id": "child"}, definitions, parent)


def test_moa_resume_rejects_authentication_header_rotation(monkeypatch):
    from agent import moa_loop
    auth = {"value": "first-header"}
    preset = {"reference_models": [{"provider": "p1", "model": "r1"}],
              "aggregator": {"provider": "pa", "model": "a1"}}
    monkeypatch.setattr(moa_loop, "_resolve_preset_cached", lambda _name: (preset, {}))
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model,
        "base_url": f"https://{requested}.fixture/v1", "api_key": f"key-{requested}",
        "api_mode": "chat_completions", "request_overrides": {
            "extra_headers": {"Authorization": auth["value"]},
        },
    })
    metadata = moa_loop.snapshot_moa_preset("review").metadata()
    auth["value"] = "second-header"
    with pytest.raises(ValueError, match="frozen authority"):
        moa_loop.restore_moa_preset(metadata)


def test_resumable_metadata_records_active_fallback_pool_rotation():
    from tools.delegate_tool import _refresh_resumable_launch_metadata

    launch = {
        "provider": "primary", "model": "main", "credential_pool_entry_id": "primary-account",
        "fallbacks": [{
            "provider": "fallback", "model": "backup", "authority_fingerprint": "launch-digest",
        }],
    }
    child = SimpleNamespace(
        provider="fallback", model="backup", api_key="rotated-fallback-secret",
        _credential_pool_entry_id="fallback-account-b",
    )
    updated = _refresh_resumable_launch_metadata(child, launch)

    assert updated["credential_pool_entry_id"] == "primary-account"
    assert updated["fallbacks"][0]["credential_pool_entry_id"] == "fallback-account-b"
    assert updated["fallbacks"][0]["authority_fingerprint"] == hashlib.sha256(
        b"rotated-fallback-secret"
    ).hexdigest()
    assert "rotated-fallback-secret" not in json.dumps(updated)


def test_fallback_resume_restores_persisted_stable_pool_entry():
    from tools.custom_subagents import ResolvedRoute
    from tools.delegate_tool import _fallback_metadata_matches, _restore_fallback_authority
    from hermes_cli.route_identity import normalize_route_base_url

    entry = SimpleNamespace(
        id="fallback-account-b", provider="fallback",
        runtime_api_key="refreshed-fallback-token", runtime_base_url="https://fallback.fixture/v1",
    )
    pool = SimpleNamespace(entries=lambda: [entry])
    launch_key = "launch-fallback-token"
    route = ResolvedRoute(
        "fallback", "backup", "https://fallback.fixture/v1", "chat_completions", None,
        launch_key, hashlib.sha256(launch_key.encode()).hexdigest(), "{}", pool,  # type: ignore[arg-type]
    )
    expected = [route.metadata()]
    expected[0]["credential_pool_entry_id"] = entry.id
    expected[0]["authority_fingerprint"] = hashlib.sha256(
        entry.runtime_api_key.encode()
    ).hexdigest()

    restored = _restore_fallback_authority((route,), expected, normalize_route_base_url)

    assert restored[0].api_key == entry.runtime_api_key
    assert restored[0].credential_pool_entry_id == entry.id
    assert _fallback_metadata_matches(restored, expected)
    assert entry.runtime_api_key not in json.dumps(expected)
