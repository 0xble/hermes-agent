"""Regression coverage for resilient model-map admission and fallback cooldowns."""

from types import SimpleNamespace

from agent.rate_limit_tracker import RateLimitBucket, RateLimitState, tpm_admission_delay
from agent.retry_utils import MINUTE_RATE_LIMIT_COOLDOWN_CAP_S, provider_retry_after_seconds, tpm_overage_cooldown_seconds
from agent.turn_failure_copy import exhausted_copy
from hermes_cli.config_value_checks import _validate_model_maps
from hermes_cli.config_migrations_delegation_roles import role_settings
from hermes_cli.config import validate_config_structure


def test_role_model_map_has_three_independent_fallback_domains():
    issues = validate_config_structure({"delegation": {"backends": {"model_map": role_settings()}}})
    assert not issues, [issue.message for issue in issues]


def test_upstream_family_is_judged_by_model_not_provider_label_with_explicit_override():
    from hermes_cli.fallback_config import upstream_family
    proxy = "custom:openai-proxy"
    assert upstream_family({"provider": proxy, "model": "gpt-6.1-sol"}) == "openai"
    assert upstream_family({"provider": proxy, "model": "muse-wonder-full-1.0"}) == "meta"
    assert upstream_family({"provider": "custom:claude-proxy", "model": "claude-sonnet-5-5"}) == "anthropic"
    assert upstream_family({"provider": "xai-oauth", "model": "grok-4.7"}) == "xai"
    assert upstream_family({"provider": proxy, "model": "muse-x", "upstream": "Custom-Vendor"}) == "custom-vendor"


def _user_option(chain):
    return {"name": "coder", "description": "x", "provider": "custom:openai-proxy",
            "model": "gpt-6.1-sol", "fallback_providers": chain}


def test_migration_keeps_a_user_chain_that_already_meets_the_bar():
    from hermes_cli.config_migrations_delegation_roles import install_role_routes
    chain = [
        {"provider": "custom:claude-proxy", "model": "claude-opus-5-5"},
        {"provider": "xai-oauth", "model": "grok-4.7"},
        {"provider": "custom:openai-proxy", "model": "muse-wonder-full-1.0"},
    ]
    config = {"delegation": {"backends": {"model_map": {"options": [_user_option(chain)]}}}}
    changes, aliases = [], {}
    install_role_routes(config, aliases, changes)
    kept = config["delegation"]["backends"]["model_map"]["options"]
    assert [o["name"] for o in kept] == ["coder"]
    assert [r["model"] for r in kept[0]["fallback_providers"]] == [r["model"] for r in chain]
    assert kept[0]["model"] == "gpt-6.1-sol" and aliases == {}
    assert not any("installed upstream-aware" in c for c in changes)


def test_migration_replaces_a_user_chain_that_fails_the_bar():
    from hermes_cli.config_migrations_delegation_roles import install_role_routes
    config = {"delegation": {"backends": {"model_map": {"options": [_user_option(
        [{"provider": "custom:openai-proxy", "model": "gpt-6.1-terra"}])]}}}}
    changes = []
    install_role_routes(config, {}, changes)
    names = [o["name"] for o in config["delegation"]["backends"]["model_map"]["options"]]
    assert names == ["worker", "explorer", "coder"]
    assert any("installed upstream-aware" in c for c in changes)


def test_default_chains_put_the_spares_last():
    for option in role_settings()["options"]:
        models = [r["model"] for r in option["fallback_providers"]]
        assert models[-2:] == ["muse-wonder-full-1.0", "grok-4.7"], (option["name"], models)


def test_model_map_rejects_chain_with_only_one_upstream_family():
    config = {"delegation": {"backends": {"model_map": {"options": [{
        "provider": "custom:openai-proxy", "model": "gpt-primary",
        "fallback_providers": [
            {"provider": "custom:openai-proxy", "model": "muse-wonder-full-1.0"},
            {"provider": "custom:openai-proxy", "model": "gpt-other"},
            {"provider": "custom:openai-proxy", "model": "gpt-third"},
        ],
    }]}}}}
    issues = []
    _validate_model_maps(config, issues)
    assert any("upstream families" in issue.message for issue in issues)


def test_model_map_rejects_exact_primary_route_duplicate():
    config = {"delegation": {"backends": {"model_map": {"options": [{
        "provider": "custom:openai-proxy", "model": "gpt-primary",
        "fallback_providers": [
            {"provider": "custom:openai-proxy", "model": "gpt-primary"},
            {"provider": "custom:claude-proxy", "model": "claude-sonnet"},
            {"provider": "xai-oauth", "model": "grok-4.7"},
        ],
    }]}}}}
    issues = []
    _validate_model_maps(config, issues)
    assert any("duplicates the primary route" in issue.message for issue in issues)


def test_same_proxy_muse_route_is_not_skipped_when_gpt_route_is_cooled():
    from agent.chat_completion_helpers import _should_skip_fallback_candidate

    proxy = "http://127.0.0.1:8317/v1"
    agent = SimpleNamespace(
        provider="custom:openai-proxy", model="gpt-6.1-terra", base_url=proxy,
        _fallback_chain=[], _credential_pool=None,
    )
    muse = {"provider": "custom:openai-proxy", "model": "muse-wonder-full-1.0", "base_url": proxy}
    assert not _should_skip_fallback_candidate(
        agent, muse, ("custom:openai-proxy", "muse-wonder-full-1.0", proxy),
        "custom:openai-proxy", "muse-wonder-full-1.0", set())


def test_absurd_retry_after_on_minute_window_is_bounded():
    error = SimpleNamespace(response=SimpleNamespace(headers={
        "Retry-After": str(138 * 3600), "x-ratelimit-reset-requests": "60s",
    }), body={})
    assert provider_retry_after_seconds(error) == MINUTE_RATE_LIMIT_COOLDOWN_CAP_S


def test_explicit_tpm_overage_arms_one_minute_window_not_retry_after_prose():
    error = SimpleNamespace(
        message="Limit 100000, Used 99000, Requested 5000. Try again in 138h46m.",
        body={}, response=None,
    )
    assert tpm_overage_cooldown_seconds(error) == 60.0


def test_tpm_admission_blocks_request_that_will_not_fit():
    state = RateLimitState(
        captured_at=__import__("time").time(),
        tokens_min=RateLimitBucket(limit=100_000, remaining=1_000, reset_seconds=60,
                                   captured_at=__import__("time").time()),
    )
    assert 0 < tpm_admission_delay(state, 20_000) <= 60
    assert tpm_admission_delay(state, 500) == 0


def test_failure_copy_names_specific_model_map_recovery():
    text = exhausted_copy("rate_limit", label="Provider", attempts=1, summary="rate limited", reset_seconds=60,
                          delegation_role="coder", exhausted_routes=["custom:openai-proxy:gpt-6.1-sol"])
    assert "delegation.backends.model_map.options[coder].fallback_providers" in text
    assert "custom:openai-proxy:gpt-6.1-sol" in text


def _guard_agent(fallback_ok):
    import time
    calls = []
    now = time.time()
    state = RateLimitState(
        captured_at=now, provider="custom:openai-proxy",
        tokens_min=RateLimitBucket(limit=100_000, remaining=1_000, reset_seconds=60, captured_at=now),
    )
    agent = SimpleNamespace(
        provider="custom:openai-proxy", _rate_limit_state=state,
        _buffer_diagnostic_status=lambda *_a: None, _flush_status_buffer=lambda: None,
        _try_activate_fallback=lambda: calls.append("fallback") or fallback_ok,
    )
    return agent, calls


def _run_guard(agent, tokens, monkeypatch):
    from agent.turn_api_call import nous_rate_limit_guard
    monkeypatch.setattr("agent.conversation_loop._arm_fallback_restart", lambda *a: "sys")
    return nous_rate_limit_guard(
        agent, _retry=None, api_messages=[], messages=[], conversation_history=[],
        active_system_prompt="sys", retry_count=0, compression_attempts=0, api_call_count=0,
        approx_tokens=tokens,
    )


def test_dispatch_guard_switches_route_before_oversized_request(monkeypatch):
    agent, calls = _guard_agent(True)
    assert _run_guard(agent, 20_000, monkeypatch).action == "break"
    assert calls == ["fallback"]


def test_dispatch_guard_fails_closed_without_fallback(monkeypatch):
    agent, _ = _guard_agent(False)
    verdict = _run_guard(agent, 20_000, monkeypatch)
    assert verdict.action == "return" and verdict.result["failed"] is True


def test_dispatch_guard_allows_request_that_fits(monkeypatch):
    agent, calls = _guard_agent(True)
    assert _run_guard(agent, 500, monkeypatch).action == "fallthrough"
    assert calls == []
