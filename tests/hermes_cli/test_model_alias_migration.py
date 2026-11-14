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
        "delegation": {"model_preset": "delegation-sol", "by_parent": {"@uncensored": "inherit"},
                       "inherit_service_tier": True},
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
    assert route["default"] == "worker"
    assert after["delegation"]["inherit_service_tier"] is False
    assert [(o["name"], after["model_aliases"][o["model"]]["model"], o["reasoning_effort"]) for o in route["options"]] == [
        ("worker", "gpt-6-luna", "low"), ("explorer", "gpt-6.1-terra", "medium"),
        ("coder", "gpt-6.1-sol", "high"),
    ]
    assert all(o["provider"] == "custom:openai-proxy" and o["service_tier"] == "normal" for o in route["options"])
    assert all("service_tier" not in entry for o in route["options"] for entry in o["fallback_providers"])
    assert [entry["model"] for entry in route["options"][2]["fallback_providers"]] == [
        "claude-sonnet-5-5", "muse-wonder-full-1.0", "grok-4.7"
    ]
    assert [entry["model"] for entry in after["cron"]["fallback_providers"]] == [
        "claude-sonnet-5-5", "muse-wonder-full-1.0", "grok-4.7"
    ]
    assert all(entry["model"] != "claude-opus-5-5" for entry in after["cron"]["fallback_providers"])
    assert "inherit_parent_aliases" not in after["delegation"]
    assert "advanced-delegation" in after["plugins"]["enabled"]
    assert after["model_aliases"]["attended"]["model"] == "opus"
    assert "gpt-6.1-sol" not in after["model_aliases"]
    assert changes
    again, changes = migrate_document(after)
    assert again == after and changes == []


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_store_dry_run_and_apply_rewrite_jobs_and_topics(tmp_path, monkeypatch, encoding):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg, _ = migrate_document(_config())
    paths = {"cron/jobs.json": {"revision": "keep", "jobs": [{"id": "one", "model": "@attended", "provider": "stale"}]},
             "sessions/sessions.json": {"topic": {"model_override": {"model": "@attended", "provider": "stale"}}}}
    for relative, document in paths.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding=encoding)
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


def test_alias_migration_preserves_proxy_credentials_and_fallback_policy():
    before = _config()
    before['model'].update(default='gpt-6.1-sol', provider='custom:openai-proxy',
                           api_key='test-key', base_url='https://proxy.invalid/v1')
    before['model_presets']['delegation-sol']['provider'] = 'custom:openai-proxy'
    before['model_presets']['delegation-sol']['model'] = 'gpt-6.1-sol'
    before['fallback_providers'] = [{'provider': 'openai', 'model': 'paid'}]
    after, _ = migrate_document(before)
    assert after['model'] == before['model']
    assert after['fallback_providers'] == before['fallback_providers']
    route = after['delegation']['backends']['model_map']['options'][0]
    assert after['model_aliases']['delegation-sol']['provider'] == 'custom:openai-proxy'
    assert route['provider'] == 'custom:openai-proxy'
    assert route['fallback_providers'][0]['model'] == 'gpt-6.1-terra'


def test_existing_model_map_migration_preserves_explicit_empty_cron_fallbacks():
    before, _ = migrate_document(_config())
    before["cron"] = {"fallback_providers": []}
    after, changes = migrate_document(before)
    assert after["cron"]["fallback_providers"] == []
    assert not any("cron.fallback_providers" in change for change in changes)


def test_existing_model_map_migration_preserves_custom_cron_fallbacks():
    custom = [{"provider": "nous", "model": "x"}]
    before, _ = migrate_document(_config())
    before["cron"] = {"fallback_providers": custom}
    after, changes = migrate_document(before)
    assert after["cron"]["fallback_providers"] == custom
    assert not any("cron.fallback_providers" in change for change in changes)


def test_existing_model_map_migration_preserves_explicit_none_cron_fallbacks():
    before, _ = migrate_document(_config())
    before["cron"] = {"fallback_providers": None}
    after, changes = migrate_document(before)
    assert after["cron"]["fallback_providers"] is None
    assert not any("cron.fallback_providers" in change for change in changes)


def test_existing_model_map_migration_repairs_cron_opus_default():
    before, _ = migrate_document(_config())
    before["cron"] = {"fallback_providers": [
        {"provider": "custom:claude-proxy", "model": "claude-opus-5-5"},
        {"provider": "custom:openai-proxy", "model": "old-fallback"},
    ]}
    after, changes = migrate_document(before)
    assert [entry["model"] for entry in after["cron"]["fallback_providers"]] == [
        "claude-sonnet-5-5", "muse-wonder-full-1.0", "grok-4.7"
    ]
    assert after["cron"]["fallback_providers"][-1]["model"] == "grok-4.7"
    assert any("cron.fallback_providers" in change for change in changes)


def test_retirement_installs_missing_cron_fallbacks():
    after, changes = migrate_document(_config())
    assert [entry["model"] for entry in after["cron"]["fallback_providers"]] == [
        "claude-sonnet-5-5", "muse-wonder-full-1.0", "grok-4.7"
    ]
    assert any("cron.fallback_providers" in change for change in changes)


def test_migration_persists_disabled_routes_as_comments_through_config_writer(tmp_path, monkeypatch):
    import hermes_yaml as yaml
    from hermes_cli.config import atomic_config_replace
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config()))
    after, _ = migrate_document(_config())
    atomic_config_replace(path, after)
    text = path.read_text()
    assert "# - name: critical" in text and "# - name: writer" in text
    saved = yaml.safe_load(text)
    assert len(saved["delegation"]["backends"]["model_map"]["options"]) == 3
    assert migrate_document(saved)[0] == saved
    preview = "\n".join(line.replace("# ", "", 1) if line.lstrip().startswith(("# - name:", "#   ")) else line
                        for line in text.splitlines())
    options = yaml.safe_load(preview)["delegation"]["backends"]["model_map"]["options"]
    assert {entry["name"] for entry in options} == {
        "worker", "explorer", "coder", "critical", "planner", "designer", "visual", "writer",
    }


def test_cli_dry_run_reads_bom_config_without_writing(tmp_path, capsys):
    from hermes_cli.config_migrations_model_aliases import main

    config = tmp_path / "config.yaml"
    config.write_text(json.dumps(_config()), encoding="utf-8-sig")
    before = config.read_bytes()
    assert main(["--home", str(tmp_path), "--dry-run"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert config.read_bytes() == before


def _review_chain(first_model, effort):
    return [
        {"provider": "custom:claude-proxy", "model": first_model, "reasoning_effort": effort, "note": "keep"},
        {"provider": "custom:openai-proxy", "model": "muse-wonder-full-1.0", "reasoning_effort": effort},
        {"provider": "xai-oauth", "model": "grok-4.7", "reasoning_effort": effort},
    ]


def test_review_chains_replace_opus_with_sonnet_and_keep_grok_last():
    before, _ = migrate_document(_config())
    before["auxiliary"] = {
        "review": {"model": "gpt-6.1-sol", "fallback_chain": _review_chain("claude-opus-5-5", "high")},
        "background_review": {"model": "gpt-6.1-sol", "fallback_chain": _review_chain("claude-opus-5-5", "low")},
    }
    after, changes = migrate_document(before)
    for name, effort in (("review", "high"), ("background_review", "low")):
        chain = after["auxiliary"][name]["fallback_chain"]
        assert [e["model"] for e in chain] == ["claude-sonnet-5-5", "muse-wonder-full-1.0", "grok-4.7"]
        assert all(e["reasoning_effort"] == effort for e in chain)
        assert chain[0]["note"] == "keep" and chain[0]["provider"] == "custom:claude-proxy"
        assert after["auxiliary"][name]["model"] == "gpt-6.1-sol"
        assert any(f"auxiliary.{name}.fallback_chain" in change for change in changes)
    again, changes = migrate_document(after)
    assert again == after and changes == []


@pytest.mark.parametrize("route", [
    {"model": "gpt-6.1-sol"},
    {"model": "gpt-6.1-sol", "fallback_chain": []},
    {"model": "gpt-6.1-sol", "fallback_chain": None},
    {"model": "gpt-6.1-sol", "fallback_chain": [{"provider": "custom:openai-proxy", "model": "x"}]},
    {"model": "gpt-6.1-sol", "fallback_chain": [{"provider": "other", "model": "claude-opus-5-5"}]},
    {"model": "claude-opus-5-5", "provider": "custom:claude-proxy", "fallback_chain": [
        {"provider": "custom:openai-proxy", "model": "gpt-6.1-sol"}]},
])
def test_review_migration_leaves_other_routes_alone(route):
    before, _ = migrate_document(_config())
    before["auxiliary"] = {"review": copy.deepcopy(route), "goal_judge": {"model": "claude-opus-5-5"}}
    after, changes = migrate_document(before)
    assert after["auxiliary"] == before["auxiliary"]
    assert not any("auxiliary." in change for change in changes)
