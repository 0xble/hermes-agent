"""Regression coverage for resilient model-map admission and fallback cooldowns."""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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


def test_validation_and_the_spare_guards_resolve_one_family_per_route():
    from agent.spare_guards import resolve_upstream_family
    from hermes_cli.fallback_config import upstream_family
    routes = [
        ({"provider": "openrouter", "model": "anthropic/claude-sonnet-5-5"}, "anthropic"),
        ({"provider": "openrouter", "model": "openai/gpt-6.1-sol"}, "openai"),
        ({"provider": "openrouter", "model": "x-ai/grok-4.7"}, "xai"),
        ({"provider": "openrouter", "model": "x-ai/grok-4.7", "upstream": "Elsewhere"}, "elsewhere"),
        ({"provider": "custom:openai-proxy", "model": "muse-wonder-full-1.0"}, "meta"),
        ({"provider": "xai-oauth", "model": "some-new-name"}, "xai"),
    ]
    for route, family in routes:
        assert upstream_family(route) == family, route
        assert resolve_upstream_family(route["provider"], route["model"], route) == family, route


def test_a_three_vendor_chain_on_one_aggregator_meets_the_bar():
    option = {"name": "deep", "provider": "openrouter", "model": "google/gemini-3-pro", "fallback_providers": [
        {"provider": "openrouter", "model": "anthropic/claude-sonnet-5-5"},
        {"provider": "openrouter", "model": "openai/gpt-6.1-sol"},
        {"provider": "openrouter", "model": "x-ai/grok-4.7"},
    ]}
    assert validate_config_structure({"delegation": {"backends": {"model_map": {"options": [option]}}}}) == []
    # Without the vendor the names say nothing, and one aggregator is one family.
    option["fallback_providers"] = [{"provider": "openrouter", "model": f"vendor/m-{n}"} for n in "abc"]
    option["model"] = "vendor/m-primary"
    messages = [i.message for i in validate_config_structure(
        {"delegation": {"backends": {"model_map": {"options": [option]}}}})]
    assert any("spans only 1 upstream families" in m for m in messages), messages


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


def test_migration_keeps_a_user_chain_that_fails_the_bar_and_reports_it():
    from hermes_cli.config_migrations_delegation_roles import install_role_routes
    weak = _user_option([{"provider": "custom:openai-proxy", "model": "gpt-6.1-terra"}])
    no_chain = {"name": "quick", "description": "x", "provider": "custom:openai-proxy", "model": "gpt-6.1-luna"}
    config = {"delegation": {"backends": {"model_map": {"options": [weak, no_chain], "default": "quick"}}}}
    changes, aliases = [], {}
    install_role_routes(config, aliases, changes)
    kept = config["delegation"]["backends"]["model_map"]
    # The user's map is theirs: nothing is replaced, reordered or aliased.
    assert kept == {"options": [weak, no_chain], "default": "quick"} and aliases == {}
    assert not any("installed upstream-aware" in c or "official model_map" in c for c in changes)
    # Only the option that declares a weak chain is reported; the one without a chain is not.
    reported = [c for c in changes if "kept as written" in c]
    assert reported and all("options[0].fallback_providers" in c for c in reported)
    assert any("at least 3 fallback entries" in c for c in reported)


def test_migration_keeps_a_declared_delegation_chain_beside_a_user_map():
    from hermes_cli.config_migrations_delegation_roles import install_role_routes
    declared = [dict(route) for route in _GOOD_CHAIN]

    def migrate(delegation):
        config = {"delegation": {"backends": {"model_map": {"options": [_user_option(_GOOD_CHAIN)]}}, **delegation}}
        install_role_routes(config, {}, [])
        return config["delegation"]

    assert migrate({"fallback_providers": declared})["fallback_providers"] == _GOOD_CHAIN
    # An explicit null (inherit the parent chain) is a declaration too.
    assert migrate({"fallback_providers": None})["fallback_providers"] is None
    # Nothing declared: the opt-out is still written, as before.
    assert migrate({})["fallback_providers"] == []
    # With no user map the role defaults are installed, each with its own chain, and the
    # delegation-wide list is turned off beside them.
    config = {"delegation": {"fallback_providers": declared}}
    install_role_routes(config, {}, [])
    assert config["delegation"]["fallback_providers"] == []


@pytest.mark.parametrize("delegation", [{}, {"backends": {}}, {"backends": {"model_map": {"options": []}}}])
def test_migration_installs_the_role_defaults_when_no_map_exists(delegation):
    from hermes_cli.config_migrations_delegation_roles import install_role_routes
    config, changes, aliases = {"delegation": delegation}, [], {}
    install_role_routes(config, aliases, changes)
    names = [o["name"] for o in config["delegation"]["backends"]["model_map"]["options"]]
    assert names == ["worker", "explorer", "coder"] and set(aliases) == set(names)
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


_GOOD_CHAIN = [
    {"provider": "custom:claude-proxy", "model": "claude-sonnet-5-5"},
    {"provider": "custom:openai-proxy", "model": "muse-wonder-full-1.0"},
    {"provider": "xai-oauth", "model": "grok-4.7"},
]


def test_only_an_option_that_declares_a_chain_is_validated():
    route = {"description": "x", "provider": "custom:openai-proxy", "model": "gpt-6.1-sol"}
    options = [
        {**route, "name": "absent"},
        {**route, "name": "null", "fallback_providers": None},
        {**route, "name": "opted-out", "fallback_providers": []},
        {**route, "name": "declared", "fallback_providers": _GOOD_CHAIN},
    ]
    config = {"delegation": {"backends": {"model_map": {"options": options}}}}
    assert validate_config_structure(config) == []

    options.append({**route, "name": "weak", "fallback_providers": _GOOD_CHAIN[:1]})
    messages = [issue.message for issue in validate_config_structure(config)]
    assert messages and all("options[4].fallback_providers" in message for message in messages)
    # Absent and null at the top level and under cron declare nothing either.
    assert validate_config_structure({"model": {"default": "gpt-6.1-sol"}, "fallback_providers": None,
                                      "cron": {"fallback_providers": None}}) == []


_MAIN_MODEL = {"default": "gpt-6.1-sol", "provider": "custom:openai-proxy"}
_DUPLICATE_OF_MAIN = [{"provider": "custom:openai-proxy", "model": "gpt-6.1-sol"}, *_GOOD_CHAIN[::2]]
_TWO_FAMILIES = [{"provider": "custom:openai-proxy", "model": "gpt-6.1-terra"},
                 {"provider": "custom:claude-proxy", "model": "claude-sonnet-5-5"},
                 {"provider": "custom:claude-proxy", "model": "claude-haiku-5-5"}]


@pytest.mark.parametrize("scope", ["fallback_providers", "cron.fallback_providers"])
@pytest.mark.parametrize(("chain", "problem"), [
    (_DUPLICATE_OF_MAIN, "duplicates the primary route custom:openai-proxy:gpt-6.1-sol"),
    (_TWO_FAMILIES, "spans only 2 upstream families"),
    (_GOOD_CHAIN, None),
])
def test_main_and_cron_chains_are_checked_against_the_model_default(scope, chain, problem):
    # The main model lives under ``model.default``; read as ``model.model`` the primary had no
    # name, so a duplicate went unseen and its provider label counted as a third family.
    config = {"model": dict(_MAIN_MODEL)}
    if scope.startswith("cron"):
        config["cron"] = {"fallback_providers": chain}
    else:
        config["fallback_providers"] = chain
    messages = [issue.message for issue in validate_config_structure(config)]
    if problem is None:
        assert messages == []
    else:
        assert any(message.startswith(f"{scope}[0] {problem}") or message.startswith(f"{scope} {problem}")
                   for message in messages), messages


@pytest.mark.parametrize(("delegation", "chain", "problem"), [
    ({}, _DUPLICATE_OF_MAIN, "[0] duplicates the primary route custom:openai-proxy:gpt-6.1-sol"),
    ({}, _TWO_FAMILIES, " spans only 2 upstream families"),
    ({}, _GOOD_CHAIN[:1], " needs at least 3 fallback entries"),
    ({}, _GOOD_CHAIN, None),
    ({}, [], None),
    ({}, None, None),
    # Delegation naming its own route is checked against that route, not the main one.
    ({"provider": "custom:claude-proxy", "model": "claude-sonnet-5-5"}, _GOOD_CHAIN,
     "[0] duplicates the primary route custom:claude-proxy:claude-sonnet-5-5"),
    ({"provider": "custom:claude-proxy", "model": "claude-opus-5-5"}, _DUPLICATE_OF_MAIN, None),
])
def test_a_declared_delegation_chain_is_checked(delegation, chain, problem):
    config = {"model": dict(_MAIN_MODEL), "delegation": {**delegation, "fallback_providers": chain}}
    messages = [issue.message for issue in validate_config_structure(config)]
    if problem is None:
        assert messages == []
    else:
        assert any(m.startswith(f"delegation.fallback_providers{problem}") for m in messages), messages


def test_cron_chain_is_checked_against_crons_own_model_when_it_names_one():
    config = {"model": {**_MAIN_MODEL, "base_url": "http://127.0.0.1:8317/v1"},
              "cron": {"model": "claude-sonnet-5-5", "model_provider": "custom:claude-proxy",
                       "fallback_providers": [_GOOD_CHAIN[0], {"provider": "custom:openai-proxy", "model": "gpt-6.1-sol"},
                                              _GOOD_CHAIN[2]]}}
    messages = [issue.message for issue in validate_config_structure(config)]
    assert any("cron.fallback_providers[0] duplicates the primary route custom:claude-proxy:claude-sonnet-5-5" in m
               for m in messages), messages
    # A main endpoint entry carrying the main base_url duplicates the main route, not cron's.
    config["fallback_providers"] = [{**_MAIN_MODEL, "model": "gpt-6.1-sol", "base_url": "http://127.0.0.1:8317/v1/"},
                                    *_GOOD_CHAIN[::2]]
    messages = [issue.message for issue in validate_config_structure(config)]
    assert any(m.startswith("fallback_providers[0] duplicates the primary route") for m in messages), messages


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


@pytest.mark.parametrize(("reset", "expected"), [
    ("1m30s", MINUTE_RATE_LIMIT_COOLDOWN_CAP_S), ("20ms", MINUTE_RATE_LIMIT_COOLDOWN_CAP_S),
    ("6m0s", 138 * 3600.0), ("1h0m0s", 138 * 3600.0),
])
def test_composite_reset_durations_decide_the_minute_window(reset, expected):
    error = SimpleNamespace(response=SimpleNamespace(headers={
        "Retry-After": str(138 * 3600), "x-ratelimit-reset-tokens": reset,
    }), body={})
    assert provider_retry_after_seconds(error) == expected


_OVERAGE = "Limit 100000, Used 99000, Requested 5000. Please try again in 138h46m."


@pytest.mark.parametrize(("window", "expected"), [
    ("on tokens per min (TPM): ", 60.0),
    ("on requests per min (RPM): ", 60.0),
    ("on tokens per minute: ", 60.0),
    ("on tokens per day (TPD): ", None),
    ("on requests per day (RPD): ", None),
    ("", None),
])
def test_only_a_per_minute_overage_arms_the_one_minute_window(window, expected):
    # A daily limit prints the same Limit/Used/Requested line; a 60 s cooldown there would
    # re-probe a spent daily quota every minute.
    error = SimpleNamespace(
        message=f"Rate limit reached for gpt in organization org-x {window}{_OVERAGE}", body={}, response=None)
    assert tpm_overage_cooldown_seconds(error) == expected


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


def _sans_home(text):
    """Failure copy embeds the Hermes home path; keep that path out of substring checks."""
    from hermes_constants import display_hermes_home
    return text.replace(display_hermes_home(), "<home>")


def test_chain_path_names_the_list_that_feeds_the_failing_agent():
    from agent.turn_failure_routes import fallback_chain_path
    child = SimpleNamespace(_delegate_model_map_option="coder", _delegate_depth=1, _delegate_role="leaf")
    assert "the `coder` option in `delegation.backends.model_map.options`" in fallback_chain_path(child)
    assert "leaf" not in _sans_home(fallback_chain_path(child))
    plain_child = SimpleNamespace(_delegate_model_map_option=None, _delegate_depth=2, _delegate_role="orchestrator")
    assert "`delegation.fallback_providers`" in fallback_chain_path(plain_child)
    assert "orchestrator" not in _sans_home(fallback_chain_path(plain_child))
    assert "`cron.fallback_providers`" in fallback_chain_path(SimpleNamespace(platform="cron"))
    main = _sans_home(fallback_chain_path(SimpleNamespace(_delegate_depth=0, platform="telegram")))
    assert main == "`fallback_providers` in `<home>/config.yaml`"


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

    text = _sans_home(_exhausted_card(_Child()))
    assert "the `coder` option in `delegation.backends.model_map.options`" in text
    assert "gpt-6.1-sol via custom:openai-proxy, grok-4.7 via xai-oauth" in text
    assert "claude-sonnet-5-5" not in text  # configured but never reached this episode
    assert "leaf" not in text and "hermes fallback add" not in text


def test_main_agent_failure_card_points_at_top_level_fallback_providers():
    text = _sans_home(_exhausted_card(_CardAgent()))
    assert "list backup providers under `fallback_providers` in `<home>/config.yaml`." in text
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


_GUARD_SPARE = {"provider": "custom:openai-proxy", "model": "muse-wonder-full-1.0", "base_url": "http://127.0.0.1:8317/v1"}


def _guard_agent(*, chain=(_GUARD_SPARE,), fallback_ok=True, route_scoped=True):
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
        _fallback_chain=[dict(entry) for entry in chain], _fallback_index=0, _credential_pool=None,
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


def _cool(entry):
    from agent.error_classifier import FailoverReason
    from agent.shared_primary_cooldown import arm_cooldown, canonical_route
    assert arm_cooldown(canonical_route((entry["provider"], entry["base_url"], entry["model"])),
                        reason=FailoverReason.rate_limit, reset_at=time.time() + 60, backoff_count=0, request_id="peer")


def _spare_guards(monkeypatch, tmp_path, *, cap=2, budget=30_000_000, delegated=True):
    import agent.spare_guards as guards
    monkeypatch.setattr(guards, "_state_path", lambda: tmp_path / "spare_guards.json")
    monkeypatch.setattr(guards, "_load_config", lambda: {
        "delegation": {"spare_upstreams": ["meta", "xai"], "max_concurrent_on_spare": cap},
        "fallback_budgets": {"meta": budget, "xai": budget}})
    monkeypatch.setattr(guards, "_in_delegated_child", lambda: delegated)
    return guards


def test_dispatch_guard_sends_on_the_primary_when_no_fallback_could_take_the_turn(monkeypatch, tmp_path):
    # A prediction is not a refusal: with nowhere to switch, the provider decides, and the
    # chain is not walked, so its cursor and the cooldown flags are left for a real failure.
    def sends_untouched(agent, calls):
        assert _run_guard(agent, 20_000, monkeypatch).action == "fallthrough"
        assert calls == [] and agent._fallback_index == 0
        assert not hasattr(agent, "_rate_limited_until") and not hasattr(agent, "_fallback_episode_cooled")
        assert not hasattr(agent, "_unavailable_fallback_keys")

    sends_untouched(*_guard_agent(chain=()))

    agent, calls = _guard_agent()
    agent._fallback_index = 1  # the only entry was already tried this turn
    assert _run_guard(agent, 20_000, monkeypatch).action == "fallthrough"
    assert calls == [] and agent._fallback_index == 1

    # Every remaining entry sits in a cooldown another process recorded.
    _cool(_GUARD_SPARE)
    sends_untouched(*_guard_agent())

    # The spare guards would refuse it: over its daily budget, or its child cap is full.
    other = {**_GUARD_SPARE, "model": "muse-other"}
    guards = _spare_guards(monkeypatch, tmp_path, budget=1)
    guards.record_fallback_usage(SimpleNamespace(_spare_fallback_upstream="meta", _provider_fallback_active=True,
                                                 _spare_fallback_lease=None), SimpleNamespace(input_tokens=5))
    sends_untouched(*_guard_agent(chain=(other,)))
    guards = _spare_guards(monkeypatch, tmp_path, cap=1)
    holder = SimpleNamespace(_spare_fallback_lease=None, _spare_fallback_upstream=None)
    assert guards.reserve_fallback_route(holder, other)
    sends_untouched(*_guard_agent(chain=(other,)))
    # Asking took no lease: the holder's is still the only one, and it may renew its own.
    leases = json.loads((tmp_path / "spare_guards.json").read_text(encoding="utf-8"))["leases"]
    assert list(leases["meta"]) == [holder._spare_fallback_lease[1]]
    assert guards.fallback_route_admissible(holder, other)
    guards.release_fallback_route(holder)
    agent, calls = _guard_agent(chain=(other,))
    assert _run_guard(agent, 20_000, monkeypatch).action == "break"
    assert calls == ["status", "fallback"]


def test_dispatch_guard_skips_past_a_cooled_entry_to_one_that_can_take_the_turn(monkeypatch):
    _cool(_GUARD_SPARE)
    agent, calls = _guard_agent(chain=(_GUARD_SPARE, {"provider": "custom:claude-proxy", "model": "claude-sonnet-5-5",
                                                      "base_url": "http://127.0.0.1:8318/v1"}))
    assert _run_guard(agent, 20_000, monkeypatch).action == "break"
    assert calls == ["status", "fallback"]
    # The walk itself can still come back empty (a bind that fails): the request is then sent.
    agent, calls = _guard_agent(chain=(dict(_GUARD_SPARE, model="muse-other"),), fallback_ok=False)
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
    serialized = dataclasses.asdict(agent._rate_limit_state)
    assert "route" not in serialized and "http" not in repr(serialized)


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


@pytest.mark.parametrize("cooled", [
    {_PRIMARY[2]}, {_PRIMARY[2], "claude-sonnet-5-5"}, {_PRIMARY[2], "claude-sonnet-5-5", "muse-wonder-full-1.0"},
])
def test_child_runtime_keeps_the_role_primary_even_when_it_is_cooled(monkeypatch, cooled):
    # Building the child on a spare left it no primary to return to and skipped the spare
    # guards. The cooled primary is handled where every other agent handles it: construction
    # adopts the shared cooldown (tests/agent/test_mid_turn_primary_restore.py).
    runtime = _child_runtime(monkeypatch, cooled=cooled)
    assert (runtime["provider"], runtime["base_url"], runtime["model"]) == _PRIMARY
    assert runtime["api_key"] == "primary-key"
    assert runtime["fallback_model"] == [_SPARE_A, _SPARE_B]
