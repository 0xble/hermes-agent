"""Alias use boundaries resolve current configuration and preserve route contracts."""
from types import SimpleNamespace

import hermes_yaml as yaml
import pytest

from agent.delegation_provider import DelegationProvider
from hermes_cli.model_alias_runtime import ModelAliasError, resolve_model_alias
from tools import delegate_tool_routes as routes


def test_alias_resolution_reuses_upstream_labels_and_fails_closed():
    cfg = {"model_aliases": {"worker": {"model": "sol", "provider": "custom:proxy", "reasoning_effort": "high"}}}
    assert resolve_model_alias("worker", cfg, required=True).model == "sol"
    cfg["model_aliases"]["worker"]["model"] = "luna"
    assert resolve_model_alias("worker", cfg, required=True).model == "luna"
    with pytest.raises(ModelAliasError, match="Unknown model alias"):
        resolve_model_alias("missing", cfg, required=True)


class _Options(DelegationProvider):
    name = "test-options"

    def task_parameters(self, config):
        return {"option": {"type": "string", "enum": ["worker"]}}

    def route_task(self, task, config):
        return {"model": "worker", "provider": "custom:old", "label": "worker", "reasoning_effort": "high",
                "fallback_providers": [], "service_tier": "normal"}


def test_current_alias_credentials_and_effort_reach_plugin_route_before_spawn(tmp_path, monkeypatch):
    cfg = {"model_aliases": {"worker": {"model": "sol", "provider": "custom:proxy"}}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)
    monkeypatch.setattr(routes, "_active_provider", lambda _cfg: _Options())
    seen = []

    def credentials(route, parent):
        seen.append(route)
        return {"model": route["model"], "provider": route["provider"], "request_overrides": {}}

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", credentials)
    monkeypatch.setattr("tools.delegate_tool_tuning.task_service_tier", lambda *args: {})
    parent = SimpleNamespace(reasoning_config={"enabled": True, "effort": "xhigh"},
                             reasoning_override={"enabled": True, "effort": "xhigh"})
    result, error = routes.resolve_task_routes([{"goal": "plain"}, {"goal": "routed", "option": "worker"}], {}, parent)
    assert error is None
    assert all(item.creds["model"] == "sol" and item.reasoning_effort == "high" for item in result)
    assert all(item.routing_cfg["fallback_providers"] == [] for item in result)
    assert all(route["provider"] == "custom:proxy" for route in seen)
    cfg["model_aliases"]["worker"]["model"] = "luna"
    changed, _ = routes.resolve_task_routes([{"goal": "later", "option": "worker"}], {}, parent)
    assert changed[0].creds["model"] == "luna"
    count = len(seen)
    refused, _ = routes.resolve_task_routes([{"goal": "bad", "option": "unknown"}], {}, parent)
    assert refused[0].error and refused[0].refused_entry(0)["route"]["refused"]
    assert len(seen) == count


def test_stored_cron_alias_uses_new_definition_each_fire(tmp_path, monkeypatch):
    from cron.scheduler import _load_cron_job_config
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)
    path = tmp_path / "config.yaml"
    job = {"id": "stored", "name": "stored", "model": "worker"}
    for model in ("first", "second"):
        path.write_text(yaml.safe_dump({"model_aliases": {"worker": {"model": model, "provider": "custom:proxy"}}}))
        assert _load_cron_job_config(job, job["id"], job["name"]).model == model
    assert job["model"] == "worker"
