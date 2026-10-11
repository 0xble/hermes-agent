"""Configured subscriptions cost $0 marginally; token-priced routes still retain invoices."""

import json
from types import SimpleNamespace

import pytest

from agent.usage_pricing import CanonicalUsage, estimate_usage_cost, resolve_billing_route
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB

URL = "http://127.0.0.1:8317/v1"
MODEL = "gpt-6.1-sol"


@pytest.mark.parametrize("shape", ["providers", "custom_providers"])
@pytest.mark.parametrize("provider", ["custom:codex-proxy", "custom:claude-proxy", "custom"])
def test_configured_subscription_flows_through_main_turn_and_sessiondb(tmp_path, monkeypatch, shape, provider):
    from agent import turn_usage
    from run_agent import AIAgent

    entries = {
        name: {"name": name, "base_url": URL, "billing_mode": "subscription_included"}
        for name in ("codex-proxy", "claude-proxy")
    }
    included_home, unknown_home = tmp_path / "included", tmp_path / "unknown"
    for home, config in ((included_home, {shape: entries if shape == "providers" else list(entries.values())}),
                         (unknown_home, {})):
        home.mkdir()
        (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    # One process, different profile bindings: no process-global route classification cache.
    for home, expected in ((included_home, "subscription_included"), (unknown_home, "unknown"),
                           (included_home, "subscription_included")):
        token = set_hermes_home_override(home)
        try:
            assert resolve_billing_route(MODEL, provider=provider, base_url=URL).billing_mode == expected
        finally:
            reset_hermes_home_override(token)

    token = set_hermes_home_override(included_home)
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = None
    try:
        def no_metadata(*args, **kwargs):
            raise AssertionError("subscription pricing must not fetch a price table")
        monkeypatch.setattr("agent.usage_pricing.fetch_endpoint_model_metadata", no_metadata)
        result = estimate_usage_cost(MODEL, CanonicalUsage(input_tokens=1000, output_tokens=50),
                                     provider=provider, base_url=URL)
        assert (result.status, result.source, result.amount_usd) == ("included", "none", 0)
        agent = AIAgent(api_key="fixture-placeholder", base_url=URL, provider=provider,
                        api_mode="chat_completions", model=MODEL, session_id="subscription", platform="cli",
                        session_db=db, quiet_mode=True, skip_context_files=True, skip_memory=True,
                        save_trajectories=False, enabled_toolsets=["file"])
        turn_usage.record_response_usage(
            agent, SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=50),
                                   model=MODEL, id=None),
            messages=[{"role": "user", "content": "hi"}], api_call_count=1, api_duration=0.1,
            compression_attempts=0, max_compression_attempts=3)
        db.flush_token_counts()
        for table, clause in (("sessions", "id = 'subscription'"),
                              ("session_model_usage", "session_id = 'subscription' AND task = ''")):
            row = dict(db._conn.execute(
                f"SELECT estimated_cost_usd, cost_status, cost_source, billing_mode FROM {table} WHERE {clause}"
            ).fetchone())
            assert row == {"estimated_cost_usd": 0, "cost_status": "included", "cost_source": "none",
                           "billing_mode": "subscription_included"}
    finally:
        if agent is not None:
            agent.close()
        db.close()
        reset_hermes_home_override(token)


def test_subscription_requires_exact_enabled_route_and_unambiguous_bare_custom(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(json.dumps({"providers": {
        "codex-proxy": {"base_url": URL, "billing_mode": "subscription_included"},
        "metered": {"base_url": URL},
        "disabled": {"base_url": URL, "billing_mode": "subscription_included", "enabled": False},
    }}), encoding="utf-8")
    assert resolve_billing_route(MODEL, provider="custom:codex-proxy", base_url=URL).billing_mode == "subscription_included"
    # Sharing a host/port is not subscription evidence. Paths, schemes and explicit identity matter.
    for provider, url in (("custom", URL), ("custom:metered", URL), ("custom:disabled", URL),
                          ("custom:other", URL), ("custom:codex-proxy", URL + "/other"),
                          ("custom:codex-proxy", URL.replace("http:", "https:"))):
        assert resolve_billing_route(MODEL, provider=provider, base_url=url).billing_mode == "unknown"
    # Existing automatic included routes retain their contract; this config cannot override built-ins.
    assert resolve_billing_route(MODEL, provider="openai-codex").billing_mode == "subscription_included"


@pytest.mark.parametrize("shape", ["providers", "custom_providers", "mixed"])
@pytest.mark.parametrize("included_first", [True, False])
def test_shared_display_names_do_not_hide_metered_billing(tmp_path, monkeypatch, shape, included_first):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    entries = {
        "included": {"name": "Proxy", "base_url": URL, "billing_mode": "subscription_included"},
        "metered": {"name": "Proxy", "base_url": URL + "/"},
    }
    if not included_first:
        entries = dict(reversed(list(entries.items())))
    if shape == "mixed":
        first, second = entries.items()
        config = {"custom_providers": [first[1]], "providers": {second[0]: second[1]}}
    else:
        config = {shape: entries if shape == "providers" else list(entries.values())}
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")

    usage = CanonicalUsage(input_tokens=1000, output_tokens=50, raw_usage={"cost": 0.25})
    result = estimate_usage_cost(MODEL, usage, provider="custom", base_url=URL)
    assert (result.status, result.source, result.amount_usd) == ("actual", "provider_cost_api", 0.25)
    for provider in ("custom", "local", "custom:Proxy"):
        assert resolve_billing_route(MODEL, provider=provider, base_url=URL).billing_mode == "unknown"
    # A keyed route still owns its declaration even when display names collide.
    if "included" in config.get("providers", {}):
        result = estimate_usage_cost(MODEL, usage, provider="custom:included", base_url=URL)
        assert (result.status, result.source, result.amount_usd) == ("included", "none", 0)
    if "metered" in config.get("providers", {}):
        result = estimate_usage_cost(MODEL, usage, provider="custom:metered", base_url=URL)
        assert (result.status, result.source, result.amount_usd) == ("actual", "provider_cost_api", 0.25)


@pytest.mark.parametrize("shape", ["providers", "custom_providers"])
@pytest.mark.parametrize("disabled", [False, " off ", 0])
def test_disabled_declarations_do_not_veto_included_billing(tmp_path, monkeypatch, shape, disabled):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Disabled first: it must not mask the enabled declaration in either config shape.
    entries = {
        "metered": {"name": "Proxy", "url": URL + "/", "enabled": disabled},
        "included": {"name": "Proxy", "api": URL, "billing_mode": "subscription_included"},
    }
    (home / "config.yaml").write_text(json.dumps({
        shape: entries if shape == "providers" else list(entries.values()),
    }), encoding="utf-8")
    result = estimate_usage_cost(MODEL, CanonicalUsage(raw_usage={"cost": 0.25}),
                                 provider="custom", base_url=URL)
    assert (result.status, result.source, result.amount_usd) == ("included", "none", 0)


def test_legacy_provider_migration_preserves_billing_declaration():
    from hermes_cli.config_providers import _custom_provider_entry_to_provider_config

    migrated = _custom_provider_entry_to_provider_config({
        "name": "codex-proxy", "base_url": URL, "billing_mode": "subscription_included",
    })
    assert migrated is not None
    assert migrated["billing_mode"] == "subscription_included"
