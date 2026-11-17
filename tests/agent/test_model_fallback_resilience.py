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


def test_failure_copy_names_the_owning_chain_and_only_routes_that_ran_out():
    option_path = "`fallback_providers` of the `coder` option in `delegation.backends.model_map.options`"
    text = exhausted_copy("rate_limit", label="Provider", attempts=1, summary="rate limited", reset_seconds=60,
                          chain_path=option_path,
                          exhausted_routes=["gpt-6.1-sol via custom:openai-proxy", "grok-4.7 via xai-oauth"])
    assert option_path in text
    assert "gpt-6.1-sol via custom:openai-proxy, grok-4.7 via xai-oauth" in text
    assert "hermes fallback add" not in text
    # One exhausted route is the failure itself, already named by the lead: no list.
    single = exhausted_copy("rate_limit", label="Provider", attempts=1, summary="rate limited",
                            exhausted_routes=["gpt-6.1-sol via custom:openai-proxy"])
    assert "Routes that ran out" not in single
    assert "`fallback_providers` in `" in single and "config.yaml`" in single
    assert "hermes fallback add" not in single


def test_one_off_fallback_hint_names_the_config_list():
    from agent.turn_failure_copy import site_copy
    hint = site_copy("nous_rate_limit")
    assert "`fallback_providers` in `" in hint and "config.yaml`" in hint
    assert "hermes fallback add" not in hint and "model_map" not in hint


def test_chain_path_names_the_list_that_feeds_the_failing_agent():
    from agent.turn_failure_routes import fallback_chain_path
    child = SimpleNamespace(_delegate_model_map_option="coder", _delegate_depth=1, _delegate_role="leaf")
    assert "the `coder` option in `delegation.backends.model_map.options`" in fallback_chain_path(child)
    assert "leaf" not in fallback_chain_path(child)
    plain_child = SimpleNamespace(_delegate_model_map_option=None, _delegate_depth=2, _delegate_role="orchestrator")
    assert "`delegation.fallback_providers`" in fallback_chain_path(plain_child)
    assert "orchestrator" not in fallback_chain_path(plain_child)
    assert "`cron.fallback_providers`" in fallback_chain_path(SimpleNamespace(platform="cron"))
    main = fallback_chain_path(SimpleNamespace(_delegate_depth=0, platform="telegram"))
    assert main.startswith("`fallback_providers` in `") and "delegation" not in main and "cron" not in main


def test_exhausted_routes_list_only_what_this_episode_ran_out_of(monkeypatch):
    from agent import shared_primary_probe_runtime
    from agent.turn_failure_routes import exhausted_route_labels, note_route_left, reset_routes_left

    cooled = {"cooled-model"}
    monkeypatch.setattr(shared_primary_probe_runtime, "fallback_route_is_active",
                        lambda provider, base_url, model: model in cooled)
    chain = [{"provider": "spare", "model": "cooled-model"}, {"provider": "spare", "model": "spare-a"},
             {"provider": "spare", "model": "never-reached"}]
    agent = SimpleNamespace(_fallback_chain=chain, _fallback_index=2, _provider_fallback_active=False)

    note_route_left(agent, "primary", "primary-model")
    agent._provider_fallback_active = True
    labels = exhausted_route_labels(agent, "spare", "spare-a")
    assert labels == ["primary-model via primary", "spare-a via spare", "cooled-model via spare"]
    assert not any("never-reached" in label for label in labels)

    # A new episode starts its own list; the previous episode's routes are not blamed again.
    reset_routes_left(agent)
    agent._provider_fallback_active, agent._fallback_index = False, 0
    assert exhausted_route_labels(agent, "primary", "primary-model") == ["primary-model via primary"]


def _exhausted_card(agent):
    from agent.error_classifier import classify_api_error
    from agent.turn_recovery import max_retries_exhausted_result

    class _Http(Exception):
        status_code = 503

    error = _Http("HTTP 503: upstream unavailable")
    classified = classify_api_error(error, provider=agent.provider, model=agent.model)
    return max_retries_exhausted_result(
        agent, error, classified, attempts=3, is_rate_limited=False, error_msg=str(error).lower(),
        api_kwargs=None, api_messages=[], messages=[], conversation_history=None, api_call_count=1,
        approx_tokens=10, provider=agent.provider, base_url=agent.base_url, model=agent.model,
    )["final_response"]


class _CardAgent:
    log_prefix = ""
    verbose = verbose_logging = False
    suppress_status_output = True
    max_iterations = 30
    provider, model, base_url = "xai-oauth", "grok-4.7", "https://api.x.ai/v1"
    platform = "telegram"
    _delegate_depth = 0
    _delegate_role = None
    _delegate_model_map_option = None
    _provider_fallback_active = False
    _fallback_routes_left = None
    _fallback_chain = ()
    _fallback_index = 0

    def _summarize_api_error(self, error):
        return str(error)

    def _clean_error_message(self, msg):
        return msg

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def test_delegated_child_failure_card_names_its_model_map_option_not_leaf(monkeypatch):
    from agent import shared_primary_probe_runtime
    monkeypatch.setattr(shared_primary_probe_runtime, "fallback_route_is_active", lambda *a: False)

    class _Child(_CardAgent):
        platform = "subagent"
        _delegate_depth, _delegate_role, _delegate_model_map_option = 1, "leaf", "coder"
        _provider_fallback_active = True
        _fallback_routes_left = [("custom:openai-proxy", "gpt-6.1-sol")]
        _fallback_chain = ({"provider": "xai-oauth", "model": "grok-4.7"},
                           {"provider": "custom:claude-proxy", "model": "claude-sonnet-5-5"})
        _fallback_index = 1

    text = _exhausted_card(_Child())
    assert "the `coder` option in `delegation.backends.model_map.options`" in text
    assert "gpt-6.1-sol via custom:openai-proxy, grok-4.7 via xai-oauth" in text
    assert "claude-sonnet-5-5" not in text  # configured but never reached this episode
    assert "leaf" not in text and "hermes fallback add" not in text


def test_main_agent_failure_card_points_at_top_level_fallback_providers():
    text = _exhausted_card(_CardAgent())
    assert "list backup providers under `fallback_providers` in `" in text
    assert "delegation" not in text and "Routes that ran out" not in text
    assert "hermes fallback add" not in text


def test_model_map_option_reaches_the_child_only_from_a_model_map_route():
    from tools.delegate_tool import _route_overrides
    from tools.delegate_tool_routes import TaskRoute

    creds = {"provider": "custom:openai-proxy", "base_url": "http://127.0.0.1:8317/v1", "api_key": None,
             "api_mode": None, "model": "gpt-6.1-sol"}
    assert _route_overrides(TaskRoute(creds, {}, label="coder", backend="model_map"))["model_map_option"] == "coder"
    # A profile route's label is a profile name, and the default route has none.
    assert _route_overrides(TaskRoute(creds, {}, label="coder", backend="profiles"))["model_map_option"] is None
    assert _route_overrides(TaskRoute(creds, {}))["model_map_option"] is None


def _guard_agent(*, pending=True, fallback_ok=True, route_scoped=True):
    import time
    from agent.turn_api_call_admission import remember_bucket_route
    calls = []
    now = time.time()
    state = RateLimitState(
        captured_at=now, provider="custom:openai-proxy",
        tokens_min=RateLimitBucket(limit=100_000, remaining=1_000, reset_seconds=60, captured_at=now),
    )
    agent = SimpleNamespace(
        provider="custom:openai-proxy", base_url="http://127.0.0.1:8317/v1", model="gpt-6.1-sol",
        _rate_limit_state=state,
        _buffer_diagnostic_status=lambda *_a: calls.append("status"), _flush_status_buffer=lambda: None,
        _has_pending_fallback=lambda: pending,
        _try_activate_fallback=lambda: calls.append("fallback") or fallback_ok,
    )
    if route_scoped:
        remember_bucket_route(agent, state)
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
    # limit 100000, remaining 1000, estimate 20000: the request cannot fit, so it is never sent.
    agent, calls = _guard_agent()
    assert _run_guard(agent, 20_000, monkeypatch).action == "break"
    assert calls == ["status", "fallback"]


def test_dispatch_guard_sends_when_no_fallback_is_left(monkeypatch):
    # A prediction is not a refusal: with nowhere to switch, the provider decides.
    agent, calls = _guard_agent(pending=False)
    assert _run_guard(agent, 20_000, monkeypatch).action == "fallthrough"
    assert calls == []
    agent, calls = _guard_agent(fallback_ok=False)  # every remaining entry cooled or unusable
    assert _run_guard(agent, 20_000, monkeypatch).action == "fallthrough"
    assert calls == ["status", "fallback"]


def test_dispatch_guard_allows_request_that_fits(monkeypatch):
    agent, calls = _guard_agent()
    assert _run_guard(agent, 500, monkeypatch).action == "fallthrough"
    assert calls == []


def test_dispatch_guard_fails_open_without_headers_estimate_or_matching_route(monkeypatch):
    agent, calls = _guard_agent()
    for tokens in (None, "unknown", 0):
        assert _run_guard(agent, tokens, monkeypatch).action == "fallthrough"
    agent._rate_limit_state = None
    assert _run_guard(agent, 20_000, monkeypatch).action == "fallthrough"

    # Headers whose route was never recorded describe no known bucket.
    agent, calls_unscoped = _guard_agent(route_scoped=False)
    assert _run_guard(agent, 20_000, monkeypatch).action == "fallthrough"

    # The spare shares the proxy label with the primary whose bucket is empty: not its bucket.
    agent, calls_spare = _guard_agent()
    agent.model = "muse-wonder-full-1.0"
    assert _run_guard(agent, 20_000, monkeypatch).action == "fallthrough"

    # A window that has already reset no longer predicts anything.
    agent, calls_reset = _guard_agent()
    agent._rate_limit_state.tokens_min.captured_at -= 120
    assert _run_guard(agent, 20_000, monkeypatch).action == "fallthrough"

    # A state swapped in since the pairing was recorded belongs to no known route.
    agent, calls_swapped = _guard_agent()
    agent._rate_limit_state = RateLimitState(
        captured_at=agent._rate_limit_state.captured_at, tokens_min=agent._rate_limit_state.tokens_min)
    assert _run_guard(agent, 20_000, monkeypatch).action == "fallthrough"
    assert calls == calls_unscoped == calls_spare == calls_reset == calls_swapped == []


def test_admission_route_stays_out_of_the_serialized_rate_limit_state():
    # The usage payload serializes this state; an endpoint URL must not ride along in it.
    import dataclasses
    agent, _calls = _guard_agent()
    assert "8317" not in repr(dataclasses.asdict(agent._rate_limit_state))


_PRIMARY = ("custom:openai-proxy", "http://127.0.0.1:8317/v1", "gpt-6.1-sol")
_SPARE_A = {"provider": "custom:claude-proxy", "model": "claude-sonnet-5-5", "base_url": "http://127.0.0.1:8318/v1"}
_SPARE_B = {"provider": "custom:muse-proxy", "model": "muse-wonder-full-1.0", "base_url": "http://127.0.0.1:8319/v1",
            "api_key": "spare-b-key"}


def _child_runtime(monkeypatch, cooled, chain=(_SPARE_A, _SPARE_B)):
    from agent import shared_primary_probe_runtime
    from tools.delegate_tool_config import _resolve_child_runtime

    monkeypatch.setattr(shared_primary_probe_runtime, "fallback_route_is_active",
                        lambda provider, base_url, model: model in cooled)
    parent = SimpleNamespace(model="parent-model", provider="openrouter", base_url="https://openrouter.ai/api/v1",
                             api_mode="chat_completions", acp_args=[], _fallback_chain=[])
    return _resolve_child_runtime(
        parent, {}, "parent-key", model=_PRIMARY[2], override_provider=_PRIMARY[0], override_base_url=_PRIMARY[1],
        override_api_key="primary-key", override_api_mode="chat_completions", override_acp_command=None,
        override_acp_args=None, routing_cfg={"fallback_providers": [dict(entry) for entry in chain]},
    )


def test_child_runtime_keeps_a_healthy_primary_and_its_whole_chain(monkeypatch):
    runtime = _child_runtime(monkeypatch, cooled=set())
    assert (runtime["provider"], runtime["model"], runtime["api_key"]) == (_PRIMARY[0], _PRIMARY[2], "primary-key")
    assert [entry["model"] for entry in runtime["fallback_model"]] == ["claude-sonnet-5-5", "muse-wonder-full-1.0"]


def test_child_runtime_starts_on_first_non_cooled_route_when_primary_is_cooled(monkeypatch):
    runtime = _child_runtime(monkeypatch, cooled={_PRIMARY[2]})
    assert (runtime["model"], runtime["base_url"]) == ("claude-sonnet-5-5", "http://127.0.0.1:8318/v1")
    # No key of its own: the parent's key is inherited, never the cooled primary's.
    assert runtime["api_key"] == "parent-key"
    assert [entry["model"] for entry in runtime["fallback_model"]] == ["muse-wonder-full-1.0"]

    runtime = _child_runtime(monkeypatch, cooled={_PRIMARY[2], "claude-sonnet-5-5"})
    assert (runtime["model"], runtime["api_key"]) == ("muse-wonder-full-1.0", "spare-b-key")
    assert runtime["fallback_model"] == []


def test_child_runtime_keeps_the_primary_when_every_route_is_cooled(monkeypatch):
    runtime = _child_runtime(monkeypatch, cooled={_PRIMARY[2], "claude-sonnet-5-5", "muse-wonder-full-1.0"})
    assert (runtime["provider"], runtime["model"], runtime["base_url"]) == _PRIMARY[0:1] + (_PRIMARY[2], _PRIMARY[1])
    assert runtime["api_key"] == "primary-key"
    assert [entry["model"] for entry in runtime["fallback_model"]] == ["claude-sonnet-5-5", "muse-wonder-full-1.0"]


def test_child_runtime_skips_a_spare_that_cannot_be_credentialed(monkeypatch):
    from tools import delegate_tool_config

    real = delegate_tool_config._resolve_delegation_credentials

    def resolve(cfg, parent):
        if cfg.get("model") == "claude-sonnet-5-5":
            raise ValueError("no credentials for custom:claude-proxy")
        return real(cfg, parent)

    monkeypatch.setattr(delegate_tool_config, "_resolve_delegation_credentials", resolve)
    runtime = _child_runtime(monkeypatch, cooled={_PRIMARY[2]})
    assert (runtime["model"], runtime["api_key"]) == ("muse-wonder-full-1.0", "spare-b-key")
