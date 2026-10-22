"""Preset references preserve the complete selected route at consumer boundaries."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
import hermes_yaml as yaml


BACKUP = {"provider": "openai", "model": "backup-model", "reasoning_effort": "none"}
PRESET = {"provider": "openrouter", "model": "primary-model", "reasoning_effort": "high",
          "fallbacks": [BACKUP]}
CFG = {"model_presets": {"p": PRESET}, "model": "@p"}


@pytest.mark.parametrize("cfg", [
    CFG,
    {"model": "main", "model_presets": {"b": BACKUP}, "fallback_providers": ["@b"]},
    {"model_presets": {"b": BACKUP}, "fallback_providers": [{"model_preset": "b"}],
     "fallback_model": [BACKUP, {"provider": "groq", "model": "legacy"}]},
])
def test_shared_fallback_raw_expanded_and_idempotent(cfg):
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.model_presets import expand_model_presets

    original = deepcopy(cfg)
    expanded = expand_model_presets(cfg)
    expected = get_fallback_chain(expanded)
    assert expected[0] == BACKUP
    assert get_fallback_chain(cfg) == expected
    assert expand_model_presets(expanded) == expanded
    assert get_fallback_chain({"fallback_providers": expected}) == expected
    assert cfg == original


def test_fallback_unknown_fails_closed_and_literal_ids_stay_literal():
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.model_presets import ModelPresetError
    with pytest.raises(ModelPresetError):
        get_fallback_chain({"fallback_providers": ["@missing"]})
    for model in ("@cf/vendor/model", "@hf/vendor/model"):
        entry = {"provider": "custom", "model": model}
        assert get_fallback_chain({"fallback_providers": [entry]}) == [entry]


@pytest.mark.parametrize("model", ["q", "@p"])
def test_default_resolves_top_level_scalar_through_cron(model):
    from cron.scheduler import _resolve_cron_job_config
    from hermes_cli.model_preset_runtime import resolve_model_ref
    cfg = {**CFG, "model": model}
    route = resolve_model_ref("@default", cfg)
    assert route.model == ("q" if model == "q" else "primary-model")
    if model == "@p":
        assert (route.provider, route.reasoning_effort, route.fallbacks) == (
            "openrouter", "high", (BACKUP,))
    jc = _resolve_cron_job_config({"model": "@default"}, cfg)
    assert jc.model == route.model
    assert jc.route == route


@pytest.mark.parametrize("shape", ["@p", {"model_preset": "p"}, {"default": "@p"}, {"model": "@p"}])
def test_cli_and_tui_initialization_and_turn_refresh(tmp_path, monkeypatch, shape):
    import cli
    from hermes_cli import config
    from hermes_cli.cli_config_load import load_cli_config
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
    from hermes_cli.cli_init_mixin import CLIInitMixin
    from tui_gateway import server as tui

    path = tmp_path / "config.yaml"
    cfg = {**deepcopy(CFG), "model": shape}
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_hermes_home", tmp_path)
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    monkeypatch.setattr(tui, "_active_config_path", lambda: path)
    loaded = load_cli_config()
    monkeypatch.setattr(cli, "CLI_CONFIG", loaded)
    assert loaded["model"]["default"] == "primary-model"
    init = CLIInitMixin()
    init.model = "primary-model"
    init._init_prompt_and_reasoning(None)
    assert init._fallback_model == [BACKUP]
    assert tui._load_fallback_model() == [BACKUP]

    # A real file edit must affect both cached agents on the next turn.
    updated = {**BACKUP, "model": "edited-backup"}
    cfg["model_presets"]["p"]["fallbacks"] = [updated]
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    chat = CLIChatTurnMixin()
    agent = SimpleNamespace(_fallback_chain=[BACKUP], _fallback_activated=False)
    chat._sync_fallback_chain_with_config(agent)
    assert agent._fallback_chain == [updated]
    session = {"agent": SimpleNamespace(_fallback_chain=[BACKUP], _fallback_activated=False)}
    tui._sync_agent_fallback_with_config("sid", session)
    assert session["agent"]._fallback_chain == [updated]

    # An unknown reference is not permission to erase the known-good chain.
    path.write_text("fallback_providers: ['@missing']\n", encoding="utf-8")
    chat._sync_fallback_chain_with_config(agent)
    tui._sync_agent_fallback_with_config("sid", session)
    assert agent._fallback_chain == session["agent"]._fallback_chain == [updated]


@pytest.mark.parametrize("policy,expected", [
    ({"enabled": True, "effort": "high"}, {"enabled": True, "effort": "high"}),
    ({"enabled": True, "effort": "BespokeTier"}, {"enabled": True, "effort": "BespokeTier"}),
    ({"enabled": False, "effort": "high"}, {"enabled": False}),
    (False, {"enabled": False}),
])
def test_typed_auxiliary_fallback_reaches_openrouter_wire(monkeypatch, policy, expected):
    import agent.auxiliary_client as aux
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda *_: {})
    # No SDK/network mock is needed: exercise the real provider kwargs projection.
    kwargs = aux._fallback_request_kwargs(
        aux._FallbackDestination("openrouter", "https://openrouter.ai/api/v1", None, "backup-model"),
        task="helper", messages=[{"role": "user", "content": "check"}], tools=None,
        temperature=None, max_tokens=None, effective_timeout=10, effective_extra_body={},
        reasoning_config={"enabled": True, "effort": "low", "exclude": True},
        fallback_entry={"provider": "openrouter", "model": "backup-model", "reasoning_effort": policy},
        task_config={}, apply_fast_lane=False, fb_label="fallback")
    wire = kwargs["extra_body"]["reasoning"]
    assert wire["enabled"] is expected["enabled"]
    if expected["enabled"]:
        assert wire["effort"] == expected["effort"]
    else:
        assert "effort" not in wire
    assert wire["exclude"] is True


@pytest.mark.parametrize("same_model", [False, True])
@pytest.mark.parametrize("pin", [None, "low"])
@pytest.mark.parametrize("policy,expected", [
    ("none", {"enabled": False}),
    ({"enabled": True, "effort": "BespokeTier"}, {"enabled": True, "effort": "BespokeTier"}),
    (None, {"enabled": True, "effort": "medium"}),
])
def test_cron_auth_fallback_reasoning_and_job_pin(monkeypatch, pin, policy, expected, same_model):
    from cron import scheduler
    from hermes_cli.auth import AuthError

    cfg = deepcopy(CFG)
    cfg["agent"] = {"reasoning_effort": "medium"}
    fallback = cfg["model_presets"]["p"]["fallbacks"][0]
    if same_model:
        fallback["model"] = "primary-model"
    if policy is None:
        fallback.pop("reasoning_effort")
    else:
        fallback["reasoning_effort"] = policy
    job = {"id": "job", "name": "job", "prompt": "check", "model": "@p"}
    if pin is not None:
        job["reasoning_effort"] = pin
    jc = scheduler._resolve_cron_job_config(job, cfg)
    calls = []

    def resolve(**kwargs):
        calls.append((kwargs["requested"], kwargs["target_model"]))
        if kwargs["requested"] == "openrouter":
            raise AuthError("fixture primary rejected")
        return {"provider": kwargs["requested"], "api_key": "fixture"}

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve)
    monkeypatch.setattr(scheduler, "_load_prefill_messages", lambda *_: None)
    monkeypatch.setattr(scheduler, "_load_credential_pool", lambda *_: None)
    monkeypatch.setattr(scheduler, "_init_cron_mcp_tools", lambda *_: None)
    setup = scheduler._resolve_cron_agent_setup(job, "job", "job", jc)
    assert setup.blocked is None
    assert (setup.model, setup.runtime["provider"]) == (fallback["model"], "openai")
    assert calls == [("openrouter", "primary-model"), ("openai", fallback["model"])]
    assert setup.reasoning_config == ({"enabled": True, "effort": "low"} if pin else expected)
    assert "_selected_route" not in setup.runtime


@pytest.mark.parametrize("override,expected", [
    (None, [BACKUP, {"provider": "groq", "model": "legacy"}]),
    ([], []),
    (["@b"], [BACKUP]),
    ([{"model_preset": "b"}], [BACKUP]),
    ([{"provider": "malformed"}], []),
])
def test_cron_fallback_override_keeps_registry_and_never_borrows_global(override, expected):
    from hermes_cli.fallback_config import get_cron_fallback_chain
    cfg = {"model_presets": {"b": BACKUP}, "fallback_providers": [BACKUP],
           "fallback_model": {"provider": "groq", "model": "legacy"},
           "cron": {"fallback_providers": override}}
    assert get_cron_fallback_chain(cfg) == expected
