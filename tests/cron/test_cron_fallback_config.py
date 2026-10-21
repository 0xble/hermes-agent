"""Cron-scoped fallback routing through real profile configuration (related to #78133)."""

import errno
from copy import deepcopy
from unittest.mock import Mock

import pytest
import hermes_yaml as yaml

from cron import scheduler
from cron.scheduler_preflight import _preflight_check_provider_key
from hermes_cli import runtime_provider
from hermes_cli.auth import AuthError
from hermes_cli.fallback_config import get_fallback_chain


def test_cron_at_model_is_resolved_from_fresh_config_each_fire(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "model_presets": {"coding": {"provider": "openrouter", "model": "coding-v1"}},
    }))
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    job = {"id": "at-route", "name": "at route", "prompt": "hello", "model": "@coding"}

    first = scheduler._load_cron_job_config(job, job["id"], job["name"])
    assert first.model == "coding-v1"
    cfg_path.write_text(yaml.safe_dump({
        "model_presets": {"coding": {"provider": "openrouter", "model": "coding-v2"}},
    }))
    second = scheduler._load_cron_job_config(job, job["id"], job["name"])
    assert second.model == "coding-v2"
    assert job["model"] == "@coding"
    # The route's provider must survive an unset cron.model_provider on every fire.
    assert first.cron_default_provider == "openrouter"
    assert second.cron_default_provider == "openrouter"


def test_cron_model_provider_does_not_override_job_route_provider(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "model_presets": {"coding": {"provider": "openrouter", "model": "coding-v1"}},
        "cron": {"model_provider": "nous"},
    }))
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    job = {"id": "r", "name": "r", "prompt": "hi", "model": "@coding"}
    assert scheduler._load_cron_job_config(job, "r", "r").cron_default_provider == "openrouter"


def _preset_job_setup(tmp_path, monkeypatch, cfg, job):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(scheduler, "_hermes_home", tmp_path)
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **kw: {
        "provider": kw["requested"], "api_key": "test-key", "api_mode": "chat_completions",
    })
    monkeypatch.setattr(scheduler, "_load_credential_pool", lambda *a: None)
    monkeypatch.setattr(scheduler, "_init_cron_mcp_tools", lambda *a: None)
    jc = scheduler._load_cron_job_config(job, job["id"], job["name"])
    return scheduler._resolve_cron_agent_setup(job, job["id"], job["name"], jc)


PRESETS = {"fast": {"provider": "openrouter", "model": "fast-model", "reasoning_effort": "low",
                    "fallbacks": [{"provider": "custom:fb", "model": "fb-model"}]}}
TOP_LEVEL_PRESETS = {"fast": {"provider": "openrouter", "model": "fast-model", "reasoning_effort": "low"}}


@pytest.mark.parametrize("model_cfg", [{"model_preset": "fast"}, "@fast"])
def test_main_preset_reasoning_and_fallbacks_reach_unpinned_cron_job(tmp_path, monkeypatch, model_cfg):
    job = {"id": "p", "name": "p", "prompt": "hi", "deliver": "local"}
    setup = _preset_job_setup(tmp_path, monkeypatch, {"model_presets": PRESETS, "model": model_cfg}, job)
    assert setup.blocked is None
    assert setup.reasoning_config == {"enabled": True, "effort": "low"}
    assert [f["model"] for f in setup.fallback_model] == ["fb-model"]


def test_top_level_fallback_providers_at_reference_reaches_cron_job(tmp_path, monkeypatch):
    cfg = {"model_presets": TOP_LEVEL_PRESETS, "model": {"provider": "openrouter", "default": "m"},
           "fallback_providers": ["@fast"]}
    job = {"id": "p", "name": "p", "prompt": "hi", "deliver": "local"}
    setup = _preset_job_setup(tmp_path, monkeypatch, cfg, job)
    assert [f["model"] for f in setup.fallback_model] == ["fast-model"]


def test_job_at_route_applies_its_own_reasoning_and_fallbacks(tmp_path, monkeypatch):
    cfg = {"model_presets": PRESETS, "model": {"provider": "nous", "default": "main"}}
    job = {"id": "p", "name": "p", "prompt": "hi", "deliver": "local", "model": "@fast"}
    setup = _preset_job_setup(tmp_path, monkeypatch, cfg, job)
    assert setup.model == "fast-model"
    assert setup.reasoning_config == {"enabled": True, "effort": "low"}
    assert [f["model"] for f in setup.fallback_model] == ["fb-model"]


GLOBAL = [{"provider": "openrouter", "model": "global-backup"}]
LEGACY = {"provider": "anthropic", "model": "legacy-backup"}
CRON = [{"provider": "custom:cron-backup", "model": "cron-backup", "reasoning_effort": "high"}]


@pytest.mark.parametrize("cron_config,expected", [
    ({}, GLOBAL + [LEGACY]),
    ({"fallback_providers": None}, GLOBAL + [LEGACY]),
    ({"fallback_providers": CRON}, CRON),
    ({"fallback_providers": []}, []),
    ({"fallback_providers": [{"provider": "missing-model"}]}, []),
])
def test_profile_chain_reaches_agent_without_changing_global(tmp_path, monkeypatch, cron_config, expected):
    cfg = {
        "model": {"provider": "openrouter", "default": "main-primary"},
        "cron": {"model": "cron-primary", "model_provider": "openrouter", **cron_config},
        "fallback_providers": GLOBAL,
        "fallback_model": LEGACY,
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "_hermes_home", tmp_path)
    # Replace external provider/MCP/pool effects, not config loading or fallback selection.
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **kw: {
        "provider": kw["requested"], "api_key": "test-key", "api_mode": "chat_completions",
    })
    monkeypatch.setattr(scheduler, "_load_credential_pool", lambda *a: None)
    monkeypatch.setattr(scheduler, "_init_cron_mcp_tools", lambda *a: None)
    job = {"id": "routing-test", "name": "routing test", "prompt": "hello", "deliver": "local"}
    jc = scheduler._load_cron_job_config(job, job["id"], job["name"])
    original = deepcopy(jc.cfg)
    setup = scheduler._resolve_cron_agent_setup(job, job["id"], job["name"], jc)
    assert setup.blocked is None
    agent_factory = Mock()
    scheduler._construct_cron_agent(
        agent_factory, job, jc.cfg, setup, workdir=None, session_id="test", session_db=None,
    )
    assert agent_factory.call_args.kwargs["model"] == cfg["cron"]["model"]
    assert agent_factory.call_args.kwargs["fallback_model"] == (expected or None)
    assert get_fallback_chain(jc.cfg) == GLOBAL + [LEGACY]
    assert jc.cfg == original


@pytest.mark.parametrize("global_chain", [GLOBAL, []])
@pytest.mark.parametrize("chain,expected", [(CRON, CRON), ([], [])])
@pytest.mark.parametrize("failure", [AuthError("missing key"), OSError(errno.ECONNREFUSED, "connection refused")])
def test_preflight_and_provider_recovery_obey_cron_chain(monkeypatch, chain, expected, failure, global_chain):
    cfg = {"cron": {"fallback_providers": chain}, "fallback_providers": global_chain}
    job = {"id": "recovery", "provider": "openrouter", "model": "primary"}
    calls = []

    def resolve(**kwargs):
        calls.append((kwargs["requested"], kwargs["target_model"]))
        if kwargs["target_model"] == job["model"]:
            raise failure
        return {"provider": kwargs["requested"]}

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", resolve)
    preflight = _preflight_check_provider_key(job, cfg)
    if expected or not isinstance(failure, AuthError):
        assert preflight is None
    else:
        assert "provider credential missing" in preflight
    calls.clear()
    jc = scheduler._CronJobConfig(cfg, job["model"], {}, "")
    if expected:
        runtime, model = scheduler._resolve_job_runtime(job, job["id"], jc)
        assert (runtime["provider"], model) == (expected[0]["provider"], expected[0]["model"])
        assert calls == [(job["provider"], job["model"]), (expected[0]["provider"], expected[0]["model"])]
    else:
        with pytest.raises(RuntimeError):
            scheduler._resolve_job_runtime(job, job["id"], jc)
        assert calls == [(job["provider"], job["model"])]
