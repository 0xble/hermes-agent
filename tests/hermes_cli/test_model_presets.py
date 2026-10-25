"""Behavioral coverage for static named model-route expansion."""
from __future__ import annotations

import copy
import pytest

import hermes_yaml as yaml

from hermes_cli.model_presets import ModelPresetError, expand_model_presets


def test_at_shorthand_expands_at_every_reference_site():
    config = {**routes(), "model": "@fast", "delegation": {"model": "@primary", "max_concurrent_children": 3},
              "auxiliary": {"compression": {"model": "@fast", "timeout": 20}},
              "fallback_providers": ["@fast"]}
    expanded = expand_model_presets(config)
    assert expanded["model"] == {"provider": "provider-c", "default": "model-c", "reasoning_effort": "minimal"}
    assert expanded["delegation"]["provider"] == "provider-a"
    assert expanded["delegation"]["max_concurrent_children"] == 3
    assert expanded["auxiliary"]["compression"]["model"] == "model-c"
    assert expanded["auxiliary"]["compression"]["timeout"] == 20
    assert expanded["fallback_providers"] == [{"provider": "provider-c", "model": "model-c", "reasoning_effort": "minimal"}]
    assert config["model"] == "@fast"


def test_load_config_expands_at_model_shorthand(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({**routes(), "model": "@fast"}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli.config import load_config
    cfg = load_config()
    assert (cfg["model"]["provider"], cfg["model"]["default"]) == ("provider-c", "model-c")


def test_documented_preset_example_expands():
    from pathlib import Path
    doc = Path(__file__).resolve().parents[2] / "website/docs/user-guide/configuring-models.md"
    block = doc.read_text().split("## Reusing a named route", 1)[1].split("```yaml", 1)[1].split("```", 1)[0]
    expanded = expand_model_presets(yaml.safe_load(block))
    assert expanded["delegation"]["fallback_providers"][0]["provider"] == "openai"


def routes():
    return {"model_presets": {
        "primary": {"provider": "provider-a", "model": "model-a", "reasoning_effort": "high", "fallbacks": [{"provider": "provider-b", "model": "model-b", "reasoning_effort": "low"}]},
        "fast": {"provider": "provider-c", "model": "model-c", "reasoning_effort": "minimal"},
    }}


def test_expands_all_runtime_consumers_without_mutating_authored_routes():
    config = {**routes(), "model": {"model_preset": "fast"}, "delegation": {"model_preset": "primary", "max_concurrent_children": 3}, "auxiliary": {"compression": {"model_preset": "primary", "timeout": 20}}, "fallback_providers": [{"model_preset": "fast"}], "moa": {"presets": {"review": {"reference_models": [{"model_preset": "fast"}], "aggregator": {"model_preset": "fast"}}}}}
    authored = copy.deepcopy(config)
    expanded = expand_model_presets(config)
    assert expanded["model"] == {"provider": "provider-c", "default": "model-c", "reasoning_effort": "minimal"}
    assert expanded["model"]["reasoning_effort"] == "minimal"
    assert "reasoning_effort" not in expanded.get("agent", {})
    assert expanded["fallback_providers"] == [{"provider": "provider-c", "model": "model-c", "reasoning_effort": "minimal"}]
    assert expanded["delegation"]["fallback_providers"][0]["provider"] == "provider-b"
    assert expanded["auxiliary"]["compression"]["fallback_chain"][0]["model"] == "model-b"
    assert expanded["moa"]["presets"]["review"]["aggregator"]["reasoning_effort"] == "minimal"
    assert config == authored


@pytest.mark.parametrize("config, fragment", [
    ({**routes(), "auxiliary": {"vision": {"model_preset": "missing"}}}, "auxiliary.vision: unknown preset 'missing'"),
    ({**routes(), "delegation": {"model_preset": "fast", "provider": "x"}}, "delegation: 'model_preset: fast' cannot be combined"),
])
def test_invalid_references_are_actionable(config, fragment):
    with pytest.raises(ModelPresetError, match=fragment):
        expand_model_presets(config)


def test_fork_moa_and_named_roles_expand_preset_fallbacks_without_dynamic_routing():
    config = {
        **routes(),
        "delegation": {"subagents": {
            "reviewer": {"description": "Review", "instructions": "Review carefully", "model_preset": "primary"},
            "no-fallback": {"description": "Review", "instructions": "Review carefully", "model_preset": "primary", "fallbacks": []},
        }},
        "moa": {"aggregator": {"model_preset": "primary"}, "reference_models": [
            {"model_preset": "primary", "fallback_models": []},
            {"provider": "inline", "model": "inline-main", "fallback_models": [{"model_preset": "fast"}]},
        ]},
    }
    expanded = expand_model_presets(config)
    reviewer = expanded["delegation"]["subagents"]["reviewer"]
    assert reviewer["provider"] == "provider-a"
    assert reviewer["fallbacks"] == [{"provider": "provider-b", "model": "model-b", "reasoning_effort": "low"}]
    assert expanded["delegation"]["subagents"]["no-fallback"]["fallbacks"] == []
    assert expanded["moa"]["aggregator"]["fallback_models"][0]["model"] == "model-b"
    assert expanded["moa"]["reference_models"][0]["fallback_models"] == []
    assert expanded["moa"]["reference_models"][1]["fallback_models"] == [{
        "provider": "provider-c", "model": "model-c", "reasoning_effort": "minimal",
    }]


def test_moa_preset_conflicts_and_nested_fallbacks_fail_strictly():
    with pytest.raises(ModelPresetError, match="cannot be combined"):
        expand_model_presets({**routes(), "moa": {"aggregator": {"model_preset": "fast", "model": "other"}}})
    with pytest.raises(ModelPresetError, match="cannot reference a preset that declares fallbacks"):
        expand_model_presets({**routes(), "moa": {"aggregator": {
            "provider": "inline", "model": "main", "fallback_models": [{"model_preset": "primary"}],
        }}})


def test_save_preserves_named_role_and_moa_fallback_references():
    from hermes_cli.model_presets import preserve_model_preset_references

    authored = {
        **routes(),
        "delegation": {"subagents": {
            "reviewer": {"description": "Review", "instructions": "Review carefully", "model_preset": "primary"},
        }},
        "moa": {"aggregator": {"model_preset": "primary"}, "reference_models": [
            {"model_preset": "primary", "fallback_models": []},
        ]},
    }
    actual = expand_model_presets(authored)
    actual["delegation"]["subagents"]["reviewer"]["description"] = "Changed description"
    actual["moa"]["aggregator"]["enabled"] = False
    restored = preserve_model_preset_references(actual, authored)
    assert restored["delegation"]["subagents"]["reviewer"] == {
        "description": "Changed description", "instructions": "Review carefully", "model_preset": "primary",
    }
    assert restored["moa"]["aggregator"] == {"enabled": False, "model_preset": "primary"}
    assert restored["moa"]["reference_models"] == [{
        "model_preset": "primary", "fallback_models": [],
    }]


def test_real_config_loader_expands_and_save_keeps_authored_reference(tmp_path, monkeypatch):
    from hermes_cli import config as config_mod
    home = tmp_path / "hermes-home"; home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    raw = {**routes(), "model": {"model_preset": "fast"}, "auxiliary": {"vision": {"model_preset": "fast"}}}
    (home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    config_mod._RAW_CONFIG_CACHE.clear(); config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    loaded = config_mod.load_config()
    assert (loaded["model"]["provider"], loaded["model"]["default"]) == ("provider-c", "model-c")
    assert loaded["auxiliary"]["vision"]["reasoning_effort"] == "minimal"
    loaded["display"]["show_thinking"] = False
    config_mod.save_config(loaded)
    saved = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"] == {"model_preset": "fast"}
    assert saved["auxiliary"]["vision"]["model_preset"] == "fast"


@pytest.mark.parametrize("task", ["vision", "goal_judge", "compression", "title_generation"])
@pytest.mark.parametrize("aux_preset", ["fast", "primary"])
def test_save_without_default_stripping_reloads_preset_and_removes_injected_route_defaults(tmp_path, monkeypatch, task, aux_preset):
    from hermes_cli import config as config_mod
    home = tmp_path / "hermes-home"; home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    raw = {**routes(), "model": {"model_preset": "fast", "context_length": 12345},
           "auxiliary": {task: {"model_preset": aux_preset}}}
    expected_auxiliary = expand_model_presets(raw)["auxiliary"][task]
    (home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    config_mod._RAW_CONFIG_CACHE.clear(); config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    loaded = config_mod.load_config()
    # Simulate defaults injected by provider/auxiliary normalization before an all-default save.
    loaded["model"].update({"base_url": "", "api_key": "", "api_mode": ""})
    loaded["auxiliary"][task].update({"base_url": "", "api_key": "", "api_mode": ""})
    config_mod.save_config(loaded, strip_defaults=False)
    saved = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"] == {"model_preset": "fast", "context_length": 12345}
    assert saved["auxiliary"][task]["model_preset"] == aux_preset
    assert not {"base_url", "api_key", "api_mode", "fallback_chain"} & set(saved["auxiliary"][task])
    config_mod._RAW_CONFIG_CACHE.clear(); config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    reloaded = config_mod.load_config()
    assert reloaded["model"]["default"] == "model-c"
    assert reloaded["auxiliary"][task]["model"] == expected_auxiliary["model"]
    assert reloaded["auxiliary"][task]["fallback_chain"] == expected_auxiliary.get("fallback_chain", [])


def test_main_reference_preserves_context_and_authored_global_route_settings():
    from hermes_cli.model_presets import preserve_model_preset_references
    authored = {**routes(), "model_presets": {**routes()["model_presets"], "plain": {"provider": "plain-p", "model": "plain-m"}},
                "model": {"model_preset": "plain", "context_length": 9000},
                "agent": {"reasoning_effort": "low"},
                "fallback_providers": [{"provider": "global", "model": "global-model"}]}
    # A separately-authored global agent/fallback is not expansion output and must survive save.
    actual = {**copy.deepcopy(authored), "model": {"provider": "plain-p", "default": "plain-m", "context_length": 12000}}
    restored = preserve_model_preset_references(actual, authored)
    assert restored["model"] == {"model_preset": "plain", "context_length": 12000}
    assert restored["agent"]["reasoning_effort"] == "low"
    assert restored["fallback_providers"] == authored["fallback_providers"]


def test_edited_main_route_flattens_instead_of_restoring_a_conflicting_reference():
    from hermes_cli.model_presets import preserve_model_preset_references
    authored = {**routes(), "model": {"model_preset": "fast", "context_length": 9000}}
    actual = {**copy.deepcopy(authored), "model": {"provider": "edited-provider", "default": "edited-model", "context_length": 12000}}
    restored = preserve_model_preset_references(actual, authored)
    assert restored["model"] == actual["model"]


@pytest.mark.parametrize("field", ["base_url", "api_base", "api_key", "api", "key_env", "api_key_env", "api_mode", "transport"])
def test_main_reference_rejects_provider_owned_fields(field):
    with pytest.raises(ModelPresetError, match="cannot be combined"):
        expand_model_presets({**routes(), "model": {"model_preset": "fast", field: "value"}})


def test_gateway_and_delegation_loaders_do_not_swallow_invalid_preset_errors(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from gateway.run import GatewayRunner
    from tools import delegate_tool_config

    err = ModelPresetError("invalid preset")
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: (_ for _ in ()).throw(err))
    with pytest.raises(ModelPresetError, match="invalid preset"):
        GatewayRunner._load_fallback_model()

    monkeypatch.setattr(delegate_tool_config.os, "environ", {}, raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: (_ for _ in ()).throw(err))
    with pytest.raises(ModelPresetError, match="invalid preset"):
        delegate_tool_config._load_config()

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"model": {"model_preset": "missing"}}), encoding="utf-8")
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = SimpleNamespace(_fallback_model=None)
    with pytest.raises(ModelPresetError, match="unknown preset"):
        GatewayRunner._refresh_fallback_model.__get__(runner)()


def test_auxiliary_fallback_reasoning_uses_route_or_retains_primary(monkeypatch):
    from agent.auxiliary_client import _fallback_reasoning_config
    primary = {"enabled": True, "effort": "high"}
    monkeypatch.setattr("agent.auxiliary_client._fallback_chain_entry", lambda *_: {"reasoning_effort": "low"})
    assert _fallback_reasoning_config("compression", "fallback_chain[0](x)", primary) == {"enabled": True, "effort": "low"}
    monkeypatch.setattr("agent.auxiliary_client._fallback_chain_entry", lambda *_: {})
    assert _fallback_reasoning_config("compression", "fallback_chain[0](x)", primary) == primary


def test_gateway_and_cron_raw_loaders_expand_temp_home_config(tmp_path, monkeypatch):
    from cron import scheduler
    from gateway import run as gateway_run
    home = tmp_path / "hermes-home"; home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({**routes(), "model": {"model_preset": "fast"}}), encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: yaml.safe_load((home / "config.yaml").read_text()))
    assert gateway_run._resolve_gateway_model(gateway_run._load_gateway_runtime_config()) == "model-c"
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: home)
    job_cfg = scheduler._load_cron_job_config({"id": "job-1"}, "job-1", "preset job")
    assert (job_cfg.model, job_cfg.model_cfg["provider"]) == ("model-c", "provider-c")


def test_missing_definitions_and_unsupported_route_keys_fail_at_the_shared_boundary():
    with pytest.raises(ModelPresetError, match="model: unknown preset 'missing'"):
        expand_model_presets({"model": {"model_preset": "missing"}})
    with pytest.raises(ModelPresetError, match="unsupported field\\(s\\): base_url"):
        expand_model_presets({"model_presets": {"bad": {"provider": "x", "model": "y", "base_url": "https://x"}}})
    with pytest.raises(ModelPresetError, match=r"fallbacks' may only be \[\]"):
        expand_model_presets({**routes(), "delegation": {"model_preset": "fast", "fallbacks": [{"provider": "x", "model": "y"}]}})


def test_explicit_empty_fallbacks_and_save_preserve_unrelated_delegation_moa_and_fallback_values():
    authored = {
        **routes(),
        "delegation": {"model_preset": "primary", "fallbacks": [], "max_concurrent_children": 2},
        "fallback_providers": [{"model_preset": "fast", "label": "cheap"}],
        "moa": {"aggregator": {"model_preset": "fast", "enabled": False}},
    }
    expanded = expand_model_presets(authored)
    main_opt_out = expand_model_presets({**routes(), "model": {"model_preset": "primary", "fallbacks": []}})
    aux_opt_out = expand_model_presets({**routes(), "auxiliary": {"vision": {"model_preset": "primary", "fallbacks": []}}})
    assert main_opt_out["fallback_providers"] == []
    assert aux_opt_out["auxiliary"]["vision"]["fallback_chain"] == []
    assert expanded["delegation"]["fallback_providers"] == []
    actual = copy.deepcopy(expanded)
    actual["delegation"]["max_concurrent_children"] = 7
    actual["fallback_providers"][0]["label"] = "re-priced"
    actual["moa"]["aggregator"]["enabled"] = True
    from hermes_cli.model_presets import preserve_model_preset_references
    restored = preserve_model_preset_references(actual, authored)
    assert restored["delegation"] == {"model_preset": "primary", "fallbacks": [], "max_concurrent_children": 7}
    assert restored["fallback_providers"] == [{"model_preset": "fast", "label": "re-priced"}]
    assert restored["moa"]["aggregator"] == {"model_preset": "fast", "enabled": True}


def test_delegation_and_fallback_consumers_receive_preset_reasoning_and_chain(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent.chat_completion_helpers import _reresolve_fallback_reasoning_config
    from tools.delegate_tool_config import _resolve_child_runtime
    cfg = expand_model_presets({**routes(), "delegation": {"model_preset": "primary"}})
    parent = SimpleNamespace(model="parent", provider="parent-provider", base_url="", api_mode="chat_completions", acp_command=None, acp_args=[], reasoning_config={"enabled": False}, _fallback_chain=[], request_overrides=None, capabilities=None)
    child = _resolve_child_runtime(parent, cfg["delegation"], "parent-key", model=None, override_provider=None, override_base_url=None, override_api_key=None, override_api_mode=None, override_acp_command=None, override_acp_args=None)
    assert child["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert child["fallback_model"] == [{"provider": "provider-b", "model": "model-b", "reasoning_effort": "low"}]
    fallback_agent = SimpleNamespace(model="model-b", reasoning_config=None)
    _reresolve_fallback_reasoning_config(fallback_agent, child["fallback_model"][0])
    assert fallback_agent.reasoning_config == {"enabled": True, "effort": "low"}


@pytest.mark.parametrize("strip_defaults", [True, False])
@pytest.mark.parametrize("edit", ["unchanged", "reasoning", "fallbacks"])
def test_main_preset_roundtrip_and_deliberate_projected_edits(tmp_path, monkeypatch, strip_defaults, edit):
    from hermes_cli import config as c
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    raw = {**routes(), "model": {"model_preset": "primary"}}
    if edit == "fallbacks":
        raw["model"]["fallbacks"] = []
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    c._RAW_CONFIG_CACHE.clear(); c._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    loaded = c.load_config()
    if edit == "reasoning":
        loaded["model"]["reasoning_effort"] = "low"
    elif edit == "fallbacks":
        loaded["fallback_providers"] = [{"provider": "edited", "model": "edited"}]
    c.save_config(loaded, strip_defaults=strip_defaults)
    saved = yaml.safe_load(path.read_text())
    assert ("model_preset" in saved["model"]) == (edit == "unchanged")
    c._RAW_CONFIG_CACHE.clear(); c._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    reloaded = c.load_config()
    from hermes_constants import resolve_reasoning_config
    assert reloaded["model"]["reasoning_effort"] == ("low" if edit == "reasoning" else "high")
    assert "reasoning_effort" not in reloaded.get("agent", {})
    assert resolve_reasoning_config(reloaded, "model-a") == {
        "enabled": True, "effort": "low" if edit == "reasoning" else "high",
    }
    assert resolve_reasoning_config(reloaded, "unrelated") is None
    assert reloaded["fallback_providers"] == loaded["fallback_providers"]


@pytest.mark.parametrize("site,field,value", [("delegation", "reasoning_effort", "low"), ("auxiliary", "api_key", "nonsecret-fixture")])
def test_new_route_fields_are_saved_inline_not_as_conflicting_reference(tmp_path, monkeypatch, site, field, value):
    from hermes_cli import config as c
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    raw: dict = {"model_presets": {"plain": {"provider": "openrouter", "model": "test/model"}}}
    raw[site] = {"model_preset": "plain"} if site == "delegation" else {"vision": {"model_preset": "plain"}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    c._RAW_CONFIG_CACHE.clear(); c._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    loaded = c.load_config()
    target = loaded[site] if site == "delegation" else loaded[site]["vision"]
    target[field] = value
    c.save_config(loaded)
    saved = yaml.safe_load(path.read_text())
    target = saved[site] if site == "delegation" else saved[site]["vision"]
    assert "model_preset" not in target
    c._RAW_CONFIG_CACHE.clear(); c._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    reloaded = c.load_config()
    target = reloaded[site] if site == "delegation" else reloaded[site]["vision"]
    assert target[field] == value


def test_main_fallback_preset_reasoning_reaches_auxiliary_wire_request(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import config as c
    from agent import auxiliary_client as a
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    raw = {"model_presets": {"fallback": {"provider": "openrouter", "model": "test/fallback", "reasoning_effort": "low"}},
           "model": {"provider": "openrouter", "default": "test/main"}, "fallback_providers": [{"model_preset": "fallback"}]}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    c._RAW_CONFIG_CACHE.clear(); c._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    client = SimpleNamespace(base_url="https://openrouter.ai/api/v1", _hermes_fallback_destination=a._FallbackDestination("openrouter", "https://openrouter.ai/api/v1", "chat_completions", "test/fallback"))
    monkeypatch.setattr(a, "_resolve_fallback_entry", lambda entry: (client, entry["model"]))
    monkeypatch.setattr(a, "_is_provider_unhealthy", lambda *args: False)
    monkeypatch.setattr(a, "_context_too_small", lambda *args, **kwargs: None)
    fb_client, model, label = a._try_main_fallback_chain("title_generation", "openrouter", failed_model="test/main")
    _, kwargs, _ = a._plan_fallback_candidate(fb_client, model, label, task="title_generation", effective_timeout=30,
        apply_fast_lane=False, messages=[{"role": "user", "content": "test"}], tools=None, temperature=None, max_tokens=50,
        effective_extra_body={}, reasoning_config={"enabled": True, "effort": "high"})
    assert kwargs["extra_body"]["reasoning"]["effort"] == "low"


def test_inline_moa_nested_fallback_references_survive_unrelated_save():
    from hermes_cli.model_presets import preserve_model_preset_references
    raw = {**routes(), "moa": {"aggregator": {"provider": "openrouter", "model": "main", "fallback_models": [{"model_preset": "fast"}]}}}
    expanded = expand_model_presets(raw)
    expanded["display"] = {"show_thinking": False}
    restored = preserve_model_preset_references(expanded, raw)
    assert restored["moa"]["aggregator"]["fallback_models"] == [{"model_preset": "fast"}]
    assert expand_model_presets(restored)["moa"] == expanded["moa"]


CF_ID = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
HF_ID = "@hf/thebloke/mistral-7b-instruct-v0.1-awq"


@pytest.mark.parametrize("value, expected", [
    ("@fast", "fast"), ("@my-route_2", "my-route_2"), (" @fast ", "fast"),
    (CF_ID, None), (HF_ID, None), ("@a.b", None), ("@a:b", None), ("@", None), ("fast", None), (None, None),
])
def test_preset_reference_rule_is_bare_name_only(value, expected):
    from hermes_cli.model_presets import preset_ref_name
    assert preset_ref_name(value) == expected


def test_literal_at_model_ids_load_and_resolve_at_every_site(tmp_path, monkeypatch):
    from hermes_cli import config as config_mod
    from hermes_cli.model_preset_runtime import resolve_model_ref, validate_model_refs
    home = tmp_path / "home"; home.mkdir()
    raw = {
        "model": {"default": CF_ID, "provider": "custom", "base_url": "https://cf.invalid/v1"},
        "fallback_providers": [{"provider": "custom", "model": HF_ID, "base_url": "https://hf.invalid/v1"}],
        "auxiliary": {"vision": {"provider": "custom", "model": CF_ID, "base_url": "https://cf.invalid/v1"}},
        "delegation": {"provider": "custom", "model": HF_ID, "base_url": "https://hf.invalid/v1"},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_mod._RAW_CONFIG_CACHE.clear(); config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    cfg = config_mod.load_config()
    assert cfg["model"]["default"] == CF_ID and cfg["model"]["base_url"] == "https://cf.invalid/v1"
    assert cfg["fallback_providers"][0]["model"] == HF_ID
    assert cfg["fallback_providers"][0]["base_url"] == "https://hf.invalid/v1"
    assert cfg["auxiliary"]["vision"]["model"] == CF_ID
    assert cfg["delegation"]["model"] == HF_ID and cfg["delegation"]["base_url"] == "https://hf.invalid/v1"
    assert "model_preset" not in cfg["model"]
    route = resolve_model_ref(CF_ID, raw)
    assert (route.name, route.model, route.provider) == ("", CF_ID, "custom")
    assert validate_model_refs(raw) == []
    # An unknown bare reference still errors.
    with pytest.raises(ModelPresetError):
        resolve_model_ref("@nope", raw)


def test_literal_at_model_id_expands_unchanged_in_string_sites():
    config = {**routes(), "model": CF_ID, "fallback_providers": [HF_ID], "delegation": {"model": HF_ID}}
    expanded = expand_model_presets(config)
    assert expanded["model"] == CF_ID
    assert expanded["delegation"]["model"] == HF_ID
    assert expanded["fallback_providers"] == [HF_ID]


def test_save_round_trip_keeps_authored_at_strings_and_injects_nothing(tmp_path, monkeypatch):
    from hermes_cli import config as config_mod
    home = tmp_path / "home"; home.mkdir()
    raw = {**routes(),
           "model": "@fast",
           "delegation": {"model": "@primary", "max_concurrent_children": 3,
                          "subagents": {"scout": {"model": "@fast"}}},
           "auxiliary": {"vision": {"model": "@fast", "timeout": 20}},
           "fallback_providers": ["@fast", "@fast"]}
    (home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_mod._RAW_CONFIG_CACHE.clear(); config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    loaded = config_mod.load_config()
    assert loaded["model"]["default"] == "model-c"
    loaded["display"]["show_thinking"] = False
    config_mod.save_config(loaded)
    saved = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"] == "@fast"
    assert saved["delegation"]["model"] == "@primary"
    assert saved["delegation"]["subagents"]["scout"]["model"] == "@fast"
    assert saved["delegation"]["max_concurrent_children"] == 3
    assert saved["auxiliary"]["vision"]["model"] == "@fast"
    assert saved["auxiliary"]["vision"]["timeout"] == 20
    assert saved["fallback_providers"] == ["@fast", "@fast"]
    assert "reasoning_effort" not in (saved.get("agent") or {})


def test_preset_effort_outranks_agent_reasoning_effort_on_every_reader(tmp_path, monkeypatch):
    """A preset that declares reasoning_effort must load beside agent.reasoning_effort.

    Precedence (session override aside): preset effort > agent.reasoning_overrides[model]
    > agent.reasoning_effort. Expansion must not raise, and load_config, the live resolver,
    and the gateway model reader must agree on the preset effort for the main route.
    """
    from gateway import run as gateway_run
    from hermes_cli.config import load_config
    from hermes_cli.model_preset_runtime import resolve_default_route

    authored = {
        "model": "@coding",
        "model_presets": {"coding": {"provider": "openrouter", "model": "coding-model", "reasoning_effort": "medium"}},
        "agent": {"reasoning_effort": "high", "reasoning_overrides": {"coding-model": "low"}},
    }
    expanded = expand_model_presets(authored)
    assert expanded["model"] == {
        "provider": "openrouter", "default": "coding-model", "reasoning_effort": "medium",
    }
    assert expanded["agent"]["reasoning_effort"] == "high"
    assert resolve_default_route(authored).reasoning_effort == "medium"

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump(authored), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    loaded = load_config()
    assert loaded["model"]["default"] == "coding-model"
    assert loaded["agent"]["reasoning_effort"] == "high"
    assert resolve_default_route(loaded).reasoning_effort == "medium"

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: yaml.safe_load((home / "config.yaml").read_text()))
    runtime = gateway_run._load_gateway_runtime_config()
    assert gateway_run._resolve_gateway_model(runtime) == "coding-model"
    assert runtime["agent"]["reasoning_effort"] == "high"
    assert resolve_default_route(runtime).reasoning_effort == "medium"


HIGH = {"enabled": True, "effort": "high"}


def _preset_high_agent_low(**model):
    """Preset declares high; agent.reasoning_effort is low and the override is low."""
    return {
        "model": model or "@coding",
        "model_presets": {
            "coding": {"provider": "openrouter", "model": "coding-model", "reasoning_effort": "high"},
        },
        "agent": {"reasoning_effort": "low", "reasoning_overrides": {"coding-model": "low"}},
    }


def test_preset_effort_agrees_across_cli_tui_cron_fallback_and_switch_back(tmp_path, monkeypatch):
    """Preset high + agent low must resolve high on every default-route reader.

    Session override stays with the caller. The shared resolver, CLI startup,
    TUI startup, a cron job on the default route, fallback activation, and
    switch_model back to the default model all agree.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent.agent_runtime_helpers import switch_model
    from agent.chat_completion_helpers import _reresolve_fallback_reasoning_config
    from cron.scheduler import _resolve_job_reasoning_config
    from hermes_cli.cli_config_load import load_cli_config
    from hermes_cli.cli_init_mixin import CLIInitMixin
    from hermes_cli.model_preset_runtime import resolve_default_route
    from hermes_constants import resolve_reasoning_config
    from tui_gateway import server as tui_server

    authored = _preset_high_agent_low()
    home = tmp_path / "agree"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump(authored), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    monkeypatch.delenv("HERMES_TUI_PROVIDER", raising=False)
    import cli as cli_mod
    monkeypatch.setattr(cli_mod, "CLI_CONFIG", load_cli_config())

    assert resolve_reasoning_config(authored) == HIGH
    assert resolve_reasoning_config(authored, "coding-model") == HIGH
    # A non-default model keeps the per-model override; the preset does not leak.
    assert resolve_reasoning_config(authored, "other-model") == {"enabled": True, "effort": "low"}

    cli_cfg = load_cli_config()
    assert resolve_default_route(cli_cfg).model == "coding-model"
    assert resolve_reasoning_config(cli_cfg, "coding-model") == HIGH

    cli = SimpleNamespace()
    CLIInitMixin._init_model_and_provider(cli, None, None, None, None)
    assert (cli.model, cli.requested_provider) == ("coding-model", "openrouter")
    CLIInitMixin._init_prompt_and_reasoning(cli, None)
    assert cli.reasoning_config == HIGH

    monkeypatch.setattr(tui_server, "_load_cfg", lambda: authored)
    assert tui_server._resolve_model() == "coding-model"
    assert tui_server._config_model_target() == ("coding-model", "openrouter")
    assert tui_server._load_reasoning_config("coding-model") == HIGH
    assert tui_server._load_reasoning_config() == HIGH

    cron = _resolve_job_reasoning_config({}, authored, "coding-model", resolve_default_route(authored))
    assert cron == HIGH

    fallback = SimpleNamespace(model="coding-model", reasoning_config=None, reasoning_override=None)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: authored)
    _reresolve_fallback_reasoning_config(fallback, {})
    assert fallback.reasoning_config == HIGH

    agent = MagicMock()
    agent.model = "other-model"
    agent.provider = "openrouter"
    agent.base_url = ""
    agent.api_mode = "chat_completions"
    agent.api_key = "k"
    agent._client_kwargs = {}
    agent._use_prompt_caching = False
    agent._use_native_cache_layout = False
    agent.reasoning_config = {"enabled": True, "effort": "low"}
    agent.reasoning_override = {"enabled": True, "effort": "low"}
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
    agent._anthropic_prompt_cache_policy = MagicMock(return_value=(False, False))
    agent._ensure_lmstudio_runtime_loaded = MagicMock()
    agent._create_openai_client = MagicMock(return_value=MagicMock())
    try:
        switch_model(agent, new_model="coding-model", new_provider="openrouter", api_key="k",
                     base_url="", api_mode="chat_completions")
    except Exception:
        pass
    assert agent.reasoning_config == HIGH


@pytest.mark.parametrize("model", ["@coding", {"default": "@coding"}, {"model_preset": "coding"}])
def test_cli_and_tui_start_on_the_preset_route(tmp_path, monkeypatch, model):
    """Both authored forms resolve to the preset's model and provider at startup."""
    from types import SimpleNamespace

    from hermes_cli.cli_config_load import load_cli_config
    from hermes_cli.cli_init_mixin import CLIInitMixin
    from tui_gateway import server as tui_server

    authored = _preset_high_agent_low()
    authored["model"] = model
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump(authored), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    import cli as cli_mod
    loaded = load_cli_config()
    monkeypatch.setattr(cli_mod, "CLI_CONFIG", loaded)

    cli = SimpleNamespace()
    CLIInitMixin._init_model_and_provider(cli, None, None, None, None)
    assert (cli.model, cli.requested_provider) == ("coding-model", "openrouter")
    assert loaded["model"]["default"] == "coding-model"
    assert loaded["model"]["provider"] == "openrouter"

    monkeypatch.setattr(tui_server, "_load_cfg", lambda: authored)
    assert tui_server._resolve_model() == "coding-model"
    assert tui_server._config_model_target() == ("coding-model", "openrouter")


def test_literal_at_model_id_passes_through_cli_and_tui_startup(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from hermes_cli.cli_init_mixin import CLIInitMixin
    from tui_gateway import server as tui_server

    cf = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    authored = {"model": {"default": cf, "provider": "custom"}}
    home = tmp_path / "literal"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump(authored), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    import cli as cli_mod
    from hermes_cli.cli_config_load import load_cli_config
    monkeypatch.setattr(cli_mod, "_hermes_home", home)
    monkeypatch.setattr(cli_mod, "CLI_CONFIG", load_cli_config())

    cli = SimpleNamespace()
    CLIInitMixin._init_model_and_provider(cli, None, None, None, None)
    assert cli.model == cf

    monkeypatch.setattr(tui_server, "_load_cfg", lambda: authored)
    assert tui_server._resolve_model() == cf
    assert tui_server._config_model_target()[0] == cf


def test_literal_at_model_id_does_not_take_the_default_preset_effort():
    """A literal ``@cf/...`` model is not the default route, even beside a preset default."""
    from hermes_constants import resolve_reasoning_config

    authored = _preset_high_agent_low()
    authored["agent"]["reasoning_overrides"]["@cf/meta/llama-3.3-70b"] = "medium"
    assert resolve_reasoning_config(authored, "@cf/meta/llama-3.3-70b") == {
        "enabled": True, "effort": "medium",
    }
    assert resolve_reasoning_config(authored, "@coding") == HIGH


def test_oneshot_resolves_preset_model_and_effort(tmp_path, monkeypatch):
    from hermes_cli.oneshot import _configured_model, _resolve_model_and_provider
    from hermes_constants import resolve_reasoning_config

    authored = _preset_high_agent_low()
    choice = _resolve_model_and_provider(authored, None, None)
    assert choice.model == "coding-model"
    assert _configured_model(authored["model"], authored) == "coding-model"
    assert resolve_reasoning_config(authored, choice.model) == HIGH


def test_agent_reasoning_effort_survives_a_preset_without_one():
    authored = {
        "model": "@coding",
        "model_presets": {"coding": {"provider": "openrouter", "model": "coding-model"}},
        "agent": {"reasoning_effort": "high"},
    }
    expanded = expand_model_presets(authored)
    assert expanded["agent"]["reasoning_effort"] == "high"


def test_save_round_trip_keeps_model_default_shorthand(tmp_path, monkeypatch):
    from hermes_cli import config as config_mod
    home = tmp_path / "home"; home.mkdir()
    raw = {**routes(), "model": {"default": "@fast", "context_length": 4096}}
    (home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_mod._RAW_CONFIG_CACHE.clear(); config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    loaded = config_mod.load_config()
    loaded["display"]["show_thinking"] = False
    config_mod.save_config(loaded)
    saved = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"] == {"default": "@fast", "context_length": 4096}
    assert "reasoning_effort" not in (saved.get("agent") or {})


@pytest.mark.parametrize("name", ["attended", "default"])
def test_builtin_names_are_reserved(name):
    config = {"model_presets": {name: {"provider": "openrouter", "model": "m"}}}
    with pytest.raises(ModelPresetError, match="reserved"):
        expand_model_presets(config)


def test_nested_presets_fail_in_static_and_runtime_resolvers():
    from hermes_cli.model_preset_runtime import resolve_model_ref
    cfg = {"model_presets": {
        "outer": {"provider": "openrouter", "model": "@inner"},
        "inner": {"provider": "openrouter", "model": "concrete"},
    }}
    with pytest.raises(ModelPresetError, match="flat"):
        expand_model_presets(cfg)
    with pytest.raises(ModelPresetError, match="flat"):
        resolve_model_ref("@outer", cfg)


@pytest.mark.parametrize("model", ["@p", {"model_preset": "p"}, {"default": "@p"}, {"model": "@p"}])
@pytest.mark.parametrize("agent", [None, {}, {"reasoning_overrides": {"m": "low", "other": "medium"}}, {"reasoning_effort": "low", "reasoning_overrides": {"m": "low"}}])
def test_main_preset_effort_stays_route_local_without_global_projection(model, agent):
    from hermes_constants import resolve_reasoning_config
    from hermes_cli.model_presets import preserve_model_preset_references

    raw = {
        "model_presets": {"p": {"provider": "openrouter", "model": "m", "reasoning_effort": "high"}},
        "model": model,
        "unrelated": {"keep": True},
    }
    if agent is not None:
        raw["agent"] = copy.deepcopy(agent)
    expanded = expand_model_presets(raw)

    assert expanded.get("agent") == raw.get("agent")
    assert expanded["model"]["reasoning_effort"] == "high"
    assert resolve_reasoning_config(raw, "m") == {"enabled": True, "effort": "high"}
    assert resolve_reasoning_config(expanded, "m") == {"enabled": True, "effort": "high"}
    assert resolve_reasoning_config(expanded, "other") == resolve_reasoning_config(raw, "other")
    assert resolve_reasoning_config(expanded, "unconfigured") == resolve_reasoning_config(raw, "unconfigured")
    assert preserve_model_preset_references(expanded, raw) == raw
    expanded.setdefault("agent", {})["reasoning_effort"] = "medium"
    saved = preserve_model_preset_references(expanded, raw)
    assert saved["model"] == model
    assert saved["agent"]["reasoning_effort"] == "medium"


@pytest.mark.parametrize("model", ["@p", {"model_preset": "p"}, {"default": "@p"}, {"model": "@p"}])
@pytest.mark.parametrize("effort", ["high", False, {"enabled": False}, {"enabled": True, "effort": "bespoke"}])
def test_unrelated_file_save_preserves_route_effort_ownership(tmp_path, monkeypatch, model, effort):
    from hermes_cli import config as config_mod
    from hermes_constants import parse_reasoning_effort, resolve_reasoning_config
    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    raw = {
        "model": model,
        "model_presets": {"p": {"provider": "openrouter", "model": "m", "reasoning_effort": effort}},
        "agent": {"reasoning_overrides": {"m": "low"}},
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    loaded = config_mod.load_config()
    import cli as cli_mod
    from hermes_cli.cli_config_load import load_cli_config
    monkeypatch.setattr(cli_mod, "_hermes_home", home)
    cli_loaded = load_cli_config()
    assert resolve_reasoning_config(cli_loaded, "m") == parse_reasoning_effort(effort)
    assert resolve_reasoning_config(loaded, "m") == parse_reasoning_effort(effort)
    assert resolve_reasoning_config(loaded, "unconfigured") is None
    loaded["display"]["show_thinking"] = False
    config_mod.save_config(loaded)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["model"] == model
    assert saved["model_presets"] == raw["model_presets"]
    assert "reasoning_effort" not in saved.get("agent", {})
    reloaded = config_mod.load_config()
    assert resolve_reasoning_config(reloaded, "m") == parse_reasoning_effort(effort)
    assert resolve_reasoning_config(reloaded, "unconfigured") is None
