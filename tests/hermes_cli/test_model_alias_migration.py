"""Retirement migrates actual route shapes without persisting concrete expansion."""
import copy
import json

import pytest

from hermes_cli.config_migrations_model_aliases import migrate_document, migrate_stores


def _config():
    return {
        "model": {"default": "sol", "attended": {"provider": "custom:claude", "model": "opus", "reasoning_effort": "high"}},
        "model_aliases": {"sol": {"provider": "custom:codex", "model": "sol-wire"}},
        "model_presets": {
            "uncensored": {"provider": "custom:venice", "model": "uncensored-wire"},
            "delegation-sol": {"provider": "custom:codex", "model": "sol-wire", "reasoning_effort": "high",
                               "fallbacks": [{"provider": "openai", "model": "paid"}]},
        },
        "delegation": {"model_preset": "delegation-sol", "by_parent": {"@uncensored": "inherit"}},
    }


def test_config_retirement_keeps_aliases_and_plugin_policy_without_mutating_input():
    before = _config()
    original = copy.deepcopy(before)
    after, changes = migrate_document(before)
    assert before == original
    assert "model_presets" not in after
    assert "by_parent" not in after["delegation"]
    assert "model_preset" not in after["delegation"]
    route = after["delegation"]["backends"]["model_map"]
    assert route["default"] == "delegation-sol"
    assert route["options"][0]["model"] == "delegation-sol"
    assert route["options"][0]["reasoning_effort"] == "high"
    assert route["options"][0]["fallback_providers"] == []
    assert after["delegation"]["inherit_parent_aliases"] == ["uncensored"]
    assert after["model_aliases"]["attended"]["model"] == "opus"
    assert changes
    again, changes = migrate_document(after)
    assert again == after and changes == []


def test_store_dry_run_and_apply_rewrite_jobs_and_topics(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg, _ = migrate_document(_config())
    paths = {"cron/jobs.json": {"revision": "keep", "jobs": [{"id": "one", "model": "@attended", "provider": "stale"}]},
             "sessions/sessions.json": {"topic": {"model_override": {"model": "@attended", "provider": "stale"}}}}
    for relative, document in paths.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding="utf-8")
    before = {relative: (tmp_path / relative).read_bytes() for relative in paths}
    assert migrate_stores(tmp_path, cfg["model_aliases"], dry_run=True)
    assert all((tmp_path / relative).read_bytes() == body for relative, body in before.items())
    migrate_stores(tmp_path, cfg["model_aliases"], dry_run=False)
    jobs = json.loads((tmp_path / "cron/jobs.json").read_text())
    assert jobs == {"revision": "keep", "jobs": [{"id": "one", "model": "attended"}]}
    topics = json.loads((tmp_path / "sessions/sessions.json").read_text())
    assert topics["topic"] == {"model_role": "attended", "model_override": None}
    assert migrate_stores(tmp_path, cfg["model_aliases"], dry_run=True) == []


def test_unknown_stored_alias_refuses_before_any_store_write(tmp_path):
    path = tmp_path / "cron/jobs.json"
    path.parent.mkdir()
    path.write_text('{"jobs": [{"model": "@missing"}]}', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unknown model alias"):
        migrate_stores(tmp_path, {}, dry_run=False)
    assert path.read_bytes() == before


def test_subscription_migration_removes_paid_proxy_credentials_and_fallbacks():
    before = _config()
    before['model'].update(default='gpt-6.1-sol', provider='custom:openai-proxy',
                           api_key='test-key', base_url='https://proxy.invalid/v1')
    before['model_presets']['delegation-sol']['provider'] = 'custom:openai-proxy'
    before['model_presets']['delegation-sol']['model'] = 'gpt-6.1-sol'
    before['fallback_providers'] = [{'provider': 'openai', 'model': 'paid'}]
    after, _ = migrate_document(before)
    assert after['model']['provider'] == 'openai-codex'
    assert 'api_key' not in after['model'] and 'base_url' not in after['model']
    assert after['fallback_providers'] == []
    route = after['delegation']['backends']['model_map']['options'][0]
    assert route['provider'] == after['model_aliases']['delegation-sol']['provider'] == 'openai-codex'
    assert route['fallback_providers'] == []
