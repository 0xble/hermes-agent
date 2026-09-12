"""Named cron routes stay references; real config/store/runtime paths, isolated homes."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
import yaml


@pytest.fixture
def routes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    from cron import scheduler
    from cron.jobs import use_cron_store
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    config = {
        "model": {"provider": "custom:global", "default": "global-model"},
        "model_presets": {
            "main": {"provider": "custom:primary", "model": "preset-model", "reasoning_effort": "high",
                     "fallbacks": [{"provider": "custom:backup", "model": "backup-model", "reasoning_effort": "low"}]},
            "fleet": {"provider": "custom:fleet", "model": "fleet-model", "reasoning_effort": "medium"},
        },
        "providers": {name: {"base_url": f"https://{name}.invalid/v1", "api_key": "test-key", "api_mode": "chat_completions"}
                      for name in ("global", "primary", "backup", "fleet")},
        "cron": {"preflight": False, "model_preset": "fleet"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    with use_cron_store(tmp_path):
        yield config, path


def test_named_job_reloads_preset_and_real_provider_runtime(routes):
    from cron.jobs import create_job, get_job
    from cron.scheduler import _load_cron_job_config, _resolve_job_runtime, _resolve_job_reasoning_config
    config, path = routes
    job = create_job("harmless fixture", "every 2h", model_preset="main")
    before = deepcopy(get_job(job["id"]))
    jc = _load_cron_job_config(job, job["id"], job["name"])
    runtime, model = _resolve_job_runtime(job, job["id"], jc)
    assert model == "preset-model"
    assert runtime["requested_provider"] == "custom:primary"
    assert runtime["base_url"] == "https://primary.invalid/v1"
    assert _resolve_job_reasoning_config(job, jc.cfg, model) == {"enabled": True, "effort": "high"}
    assert jc.cfg["fallback_providers"] == config["model_presets"]["main"]["fallbacks"]
    fallback = jc.cfg["fallback_providers"][0]
    assert _resolve_job_reasoning_config(job, jc.cfg, "backup-model", fallback) == {"enabled": True, "effort": "low"}
    config["model_presets"]["main"]["model"] = "changed-model"
    config["model_presets"]["main"]["provider"] = "custom:backup"
    config["model_presets"]["main"]["reasoning_effort"] = "low"
    path.write_text(yaml.safe_dump(config))
    jc = _load_cron_job_config(job, job["id"], job["name"])
    runtime, model = _resolve_job_runtime(job, job["id"], jc)
    assert (model, runtime["requested_provider"]) == ("changed-model", "custom:backup")
    assert get_job(job["id"]) == before
    assert before["model_preset"] == "main"
    assert before["model"] is None and before["provider"] is None


def test_fleet_reference_beats_snapshots_but_job_inline_pin_wins(routes):
    from cron.scheduler import _load_cron_job_config, _resolve_job_runtime
    job = {"id": "fixture", "name": "fixture", "model_snapshot": "old", "provider_snapshot": "old"}
    jc = _load_cron_job_config(job, "fixture", "fixture")
    runtime, model = _resolve_job_runtime(job, "fixture", jc)
    assert (model, runtime["requested_provider"]) == ("fleet-model", "custom:fleet")
    job.update(model="pinned", provider="custom:primary")
    jc = _load_cron_job_config(job, "fixture", "fixture")
    runtime, model = _resolve_job_runtime(job, "fixture", jc)
    assert (model, runtime["requested_provider"]) == ("pinned", "custom:primary")
    assert jc.cfg.get("agent", {}).get("reasoning_effort") != "medium"


@pytest.mark.parametrize("field,value", [("model", "inline"), ("provider", "openrouter"), ("reasoning_effort", "low"), ("base_url", "https://other.invalid/v1")])
def test_store_rejects_mixed_reference_atomically(routes, field, value):
    from cron.jobs import create_job, get_job, update_job
    from hermes_cli.model_presets import ModelPresetError
    with pytest.raises(ModelPresetError, match="inline route"):
        create_job("fixture", "every 2h", model_preset="main", **{field: value})
    job = create_job("fixture", "every 2h", model_preset="main")
    before = get_job(job["id"])
    with pytest.raises(ModelPresetError, match="inline route"):
        update_job(job["id"], {field: value})
    assert get_job(job["id"]) == before


def test_unknown_stored_reference_fails_closed_and_updates_validate(routes):
    from cron.jobs import create_job, update_job
    from cron.scheduler import _load_cron_job_config
    from hermes_cli.model_presets import ModelPresetError
    job = create_job("fixture", "every 2h")
    with pytest.raises(ModelPresetError, match="unknown preset"):
        update_job(job["id"], {"model_preset": "missing"})
    with pytest.raises(ModelPresetError, match="unknown preset"):
        _load_cron_job_config({**job, "model_preset": "missing"}, job["id"], job["name"])


def test_config_roundtrip_keeps_fleet_reference_and_unrelated_edit(routes):
    from hermes_cli.config import load_config, save_config, read_user_config_raw
    config, path = routes
    loaded = load_config()
    assert loaded["cron"]["model"] == "fleet-model"
    loaded["cron"]["max_concurrent_jobs"] = 3
    save_config(loaded)
    authored = read_user_config_raw(path)
    assert authored["cron"]["model_preset"] == "fleet"
    assert not authored["cron"].get("model")
    assert not authored["cron"].get("model_provider")
    assert authored["cron"]["max_concurrent_jobs"] == 3
    assert load_config()["cron"]["model"] == "fleet-model"


def test_clearing_pins_and_setting_reference_is_atomic_cli_edit(routes):
    from cron.jobs import create_job, get_job
    from hermes_cli.cron import cron_edit
    from hermes_cli.subcommands.cron import build_cron_parser
    import argparse
    parser = argparse.ArgumentParser()
    build_cron_parser(parser.add_subparsers(), cmd_cron=lambda _: None)
    job = create_job("preserve exactly", "every 480m", name="fixture", model="pinned", provider="custom:primary", reasoning_effort="medium", deliver="local")
    before = get_job(job["id"])
    args = parser.parse_args(["cron", "edit", job["id"], "--model-preset", "main", "--model", "", "--provider", "", "--reasoning-effort", ""])
    assert cron_edit(args) in (None, 0)
    after = get_job(job["id"])
    assert after["model_preset"] == "main"
    for key in before:
        if key not in {"model", "provider", "reasoning_effort", "model_snapshot", "provider_snapshot"}:
            assert after[key] == before[key]


@pytest.mark.parametrize("model", ["qwen:7b", "deepseek:latest", "kimi:latest", "vendor/model:free", "llama3:latest", "openrouter:vendor/model"])
def test_literal_cron_models_are_never_reinterpreted_as_provider_prefixes(routes, model):
    from cron.jobs import create_job
    from cron.scheduler import _load_cron_job_config, _resolve_job_runtime
    job = create_job("fixture", "every 2h", model=model, provider="custom:primary")
    jc = _load_cron_job_config(job, job["id"], "fixture")
    runtime, resolved = _resolve_job_runtime(job, job["id"], jc)
    assert resolved == model
    assert runtime["requested_provider"] == "custom:primary"


def test_tool_inference_authority_is_unchanged(routes):
    from tools.cronjob_tools import _cronjob_handler
    from cron.jobs import get_job
    result = json.loads(_cronjob_handler({"action": "create", "prompt": "fixture", "schedule": "every 2h", "model_preset": "main"}))
    assert result["success"]
    job_id = result["job"]["job_id"]
    assert not get_job(job_id).get("model_preset")


def test_tui_runtime_read_and_raw_write_keep_reference(routes):
    from tui_gateway.server import _load_cfg, _load_cfg_raw, _save_cfg
    config, path = routes
    config["model"] = {"model_preset": "main"}
    path.write_text(yaml.safe_dump(config))
    assert _load_cfg()["model"]["default"] == "preset-model"
    raw = _load_cfg_raw()
    raw["some_unrelated_setting"] = True
    _save_cfg(raw)
    assert yaml.safe_load(path.read_text())["model"] == {"model_preset": "main"}


def test_gateway_hygiene_reads_named_model(routes):
    from gateway.run_turn import GatewayTurnMixin
    config, path = routes
    config["model"] = {"model_preset": "main"}
    config["compression"] = None
    hs = SimpleNamespace(model="wrong")
    GatewayTurnMixin._hmwa_hygiene_read_config(hs, config)
    assert (hs.model, hs.provider) == ("preset-model", "custom:primary")


def test_full_scheduler_construction_uses_named_route_and_fallback_reasoning(routes, monkeypatch):
    """Exercise run_job through agent construction; only LLM and external services are doubles."""
    from unittest.mock import MagicMock, patch
    from cron.jobs import create_job, get_job
    from cron.scheduler import run_job
    from hermes_cli import runtime_provider
    from hermes_cli.auth import AuthError
    config, path = routes
    job = create_job("fixture only; no tools", "every 2h", model_preset="main")
    before = get_job(job["id"])
    real_resolve = runtime_provider.resolve_runtime_provider

    def fail_primary(**kwargs):
        if kwargs.get("requested") == "custom:primary":
            raise AuthError("fixture primary unavailable")
        return real_resolve(**kwargs)

    with patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("cron.scheduler._init_cron_mcp_tools"), \
         patch("cron.scheduler._load_credential_pool", return_value=None), \
         patch("hermes_state_registry.acquire", return_value=MagicMock()), \
         patch.object(runtime_provider, "resolve_runtime_provider", side_effect=fail_primary), \
         patch("run_agent.AIAgent") as agent_cls:
        agent_cls.return_value.run_conversation.return_value = {"final_response": "fixture completed"}
        success, output, final, error = run_job(job)
    assert success, error
    kwargs = agent_cls.call_args.kwargs
    assert kwargs["model"] == "backup-model"
    assert kwargs["requested_provider"] == "custom:backup"
    assert kwargs["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert kwargs["fallback_model"] == config["model_presets"]["main"]["fallbacks"]
    assert not agent_cls.return_value._reasoning_effort_pinned is True
    assert get_job(job["id"]) == before


def test_dashboard_create_passes_reference_to_real_store(routes, monkeypatch):
    from cron import jobs
    from hermes_cli import web_server_cron
    from hermes_cli.web_models import CronJobCreate
    config, path = routes
    monkeypatch.setattr(web_server_cron, "_cron_profile_home", lambda _: ("default", path.parent))
    monkeypatch.setattr(web_server_cron, "_mutate_cron_for_profile", lambda profile, operation, **kw: getattr(jobs, operation)(**kw))
    result = web_server_cron._create_cron_job_sync(CronJobCreate(prompt="fixture", schedule="every 2h", model_preset="main"))
    assert jobs.get_job(result["id"])["model_preset"] == "main"


def test_named_routes_do_not_report_ignored_creation_snapshot_drift():
    from hermes_cli.config import cron_model_drift_axes
    snapshots = {"model_snapshot": "old", "provider_snapshot": "old-provider"}
    current = {"current_model": "new", "current_provider": "new-provider"}
    assert cron_model_drift_axes({**snapshots, "model_preset": "main"}, **current) == []
    assert cron_model_drift_axes(snapshots, config={"cron": {"model_preset": "main"}}, **current) == []
    assert cron_model_drift_axes(snapshots, **current) == ["provider", "model"]


def test_dashboard_update_and_clear_references(routes, monkeypatch):
    from cron import jobs
    from hermes_cli.web_models import CronJobUpdate
    from hermes_cli.web_routers import cron as router
    config, path = routes
    monkeypatch.setattr(router, "_job_profile", lambda job_id, profile: "default")
    monkeypatch.setattr(router, "_cron_profile_home", lambda _: ("default", path.parent))
    def call(profile, operation, *args, **kw):
        return getattr(jobs, operation)(*args, **kw)
    monkeypatch.setattr(router, "_call_cron_for_profile", call)
    monkeypatch.setattr(router, "_mutate_cron_for_profile", call)
    job = jobs.create_job("fixture", "every 2h", model="old", provider="custom:global")
    result = router._update_cron_job_sync(job["id"], CronJobUpdate(updates={
        "model_preset": " main ", "model": None, "provider": None}))
    assert result["model_preset"] == "main"
    for key in job.keys() - {"model", "provider"}:
        assert result[key] == job[key], key
    result = router._update_cron_job_sync(job["id"], CronJobUpdate(updates={"model_preset": None}))
    assert result["model_preset"] is None


def test_disable_preset_fallbacks_and_clear_reference(routes):
    from cron.jobs import create_job, update_job
    from cron.scheduler import _load_cron_job_config
    config, path = routes
    config["fallback_model"] = {"model": "legacy-paid", "provider": "custom:global"}
    path.write_text(yaml.safe_dump(config))
    job = create_job("fixture", "every 2h", model_preset="main")
    job = update_job(job["id"], {"fallbacks": []})
    jc = _load_cron_job_config(job, job["id"], job["name"])
    assert jc.cfg["fallback_providers"] == []
    from hermes_cli.fallback_config import get_fallback_chain
    assert get_fallback_chain(jc.cfg) == []
    job = update_job(job["id"], {"model_preset": "", "model": "explicit", "provider": "custom:backup"})
    assert not job["model_preset"]
    assert _load_cron_job_config(job, job["id"], job["name"]).model == "explicit"
