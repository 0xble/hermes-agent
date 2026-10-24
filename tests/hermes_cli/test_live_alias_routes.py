"""Live aliases cross real topic, cron and child-launch boundaries without re-picking.

Only inference, process submission and unrelated cron tool discovery are stubbed.
Alias, config, credential and child route resolution run their production code.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from hermes_constants import parse_reasoning_effort


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = {
        "model": {"default": "alias:work", "provider": "openai"},
        "model_aliases": {
            "work": {"model": "gpt-test-old", "provider": "openai", "api_key": "test-old", "reasoning_effort": "low"},
        },
        "agent": {"reasoning_effort": "medium"},
        "cron": {"preflight": False},
        "delegation": {
            "backend": "by_parent", "model": "default-child", "reasoning_effort": "high",
            "backends": {"by_parent": {"routes": {"parent-live": "work", "alias:work": "inherit"}}},
        },
    }

    def save(value=None):
        if value is not None:
            config.clear()
            config.update(copy.deepcopy(value))
        (home / "config.yaml").write_text(yaml.safe_dump(config))
        return config

    save()
    return home, config, save


def _edit(config, save):
    config["model_aliases"]["work"] = {
        "model": "claude-test-new", "provider": "anthropic", "api_key": "test-new", "reasoning_effort": "high",
    }
    save()


def _parent(model="parent-live"):
    return SimpleNamespace(
        model=model, provider="custom", base_url="https://parent.test/v1", api_key="test-parent",
        api_mode="chat_completions", reasoning_config=parse_reasoning_effort("medium"),
        request_overrides={"parent_setting": True}, session_id="parent-test", _delegate_depth=0,
        _fallback_chain=[{"provider": "openai", "model": "parent-fallback"}],
        enabled_toolsets=[], disabled_toolsets=[], prefill_messages=None,
    )


def _launch(parent, **kwargs):
    from agent import subagent_lifecycle as lifecycle
    created = []

    def construct(**values):
        child = SimpleNamespace(**values)
        created.append(child)
        return child

    with patch("run_agent.AIAgent", side_effect=construct), patch.object(lifecycle._EXECUTOR, "submit"):
        service = lifecycle.SubagentLifecycleService(lambda: parent)
        handle = service.launch(lifecycle.SubagentLaunchRequest(goal="Inspect the route", **kwargs))
    with lifecycle._REGISTRY.lock:
        lifecycle._REGISTRY.records.pop(handle.subagent_id)
    return created[0]


@pytest.mark.asyncio
async def test_topic_pick_survives_alias_edit_without_consumer_rewrite(config_file):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource, SessionStore
    from hermes_cli.model_switch import switch_model
    home, config, save = config_file
    store = SessionStore(home / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", thread_id="42", chat_type="dm")
    entry = store.get_or_create_session(source)
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner._session_db = None
    runner.session_store = store
    persist = AsyncMock(side_effect=store.set_model_override)
    runner._async_session_store = SimpleNamespace(set_model_override=persist)
    runner._evict_cached_agent = MagicMock()
    runner._channel_override_for = lambda _source: None
    result = switch_model("alias:work", "openai", "before")
    assert result.success, result.error_message
    ctx = SimpleNamespace(session_key=entry.session_key, persist_global=False, current_model="before", restore_snapshot=None)
    await runner._record_model_switch(result, ctx, source=source, one_turn=False, picker=False)
    assert store.get_model_override(entry.session_key) == {"model": "alias:work"}
    persisted = entry.to_dict()
    first, first_runtime = runner._resolve_session_agent_runtime(session_key=entry.session_key)
    assert (first, first_runtime["provider"]) == ("gpt-test-old", "openai")
    assert runner._resolve_session_reasoning_config(session_key=entry.session_key, model=first) == parse_reasoning_effort("low")
    _edit(config, save)
    second, runtime = runner._resolve_session_agent_runtime(session_key=entry.session_key)
    assert (second, runtime["provider"]) == ("claude-test-new", "anthropic")
    assert runner._resolve_session_reasoning_config(session_key=entry.session_key, model=second) == parse_reasoning_effort("high")
    assert entry.to_dict() == persisted
    assert persist.call_count == 1


def _cron_fire(job):
    from cron.scheduler import _construct_cron_agent, _load_cron_job_config, _resolve_cron_agent_setup
    jc = _load_cron_job_config(job, "alias-job", "Alias job")
    with patch("cron.scheduler._init_cron_mcp_tools"), patch("cron.scheduler._load_credential_pool", return_value=None):
        setup = _resolve_cron_agent_setup(job, "alias-job", "Alias job", jc)
    assert setup.blocked is None
    ctor = MagicMock()
    _construct_cron_agent(ctor, job, jc.cfg, setup, workdir=None, session_id="cron-test", session_db=None)
    return ctor.call_args.kwargs


def test_cron_fire_resolves_alias_again_without_rewriting_job(config_file):
    _home, config, save = config_file
    job = {"id": "alias-job", "model": "alias:work"}
    original = copy.deepcopy(job)
    old = _cron_fire(job)
    assert (old["model"], old["provider"], old["reasoning_config"]) == (
        "gpt-test-old", "openai", parse_reasoning_effort("low"))
    _edit(config, save)
    new = _cron_fire(job)
    assert (new["model"], new["provider"], new["reasoning_config"]) == (
        "claude-test-new", "anthropic", parse_reasoning_effort("high"))
    assert job == original


def test_new_child_moves_existing_child_keeps_launch_route(config_file):
    _home, config, save = config_file
    parent = _parent()
    first = _launch(parent)
    assert (first.model, first.provider, first.reasoning_config) == (
        "gpt-test-old", "openai", parse_reasoning_effort("low"))
    _edit(config, save)
    second = _launch(parent)
    assert (second.model, second.provider, second.reasoning_config) == (
        "claude-test-new", "anthropic", parse_reasoning_effort("high"))
    assert (first.model, first.provider, first.reasoning_config) == (
        "gpt-test-old", "openai", parse_reasoning_effort("low"))
    assert config["delegation"]["backends"]["by_parent"]["routes"]["parent-live"] == "work"


def test_by_parent_uses_live_model_and_inherit_clears_default_route(config_file):
    _home, config, save = config_file
    parent = _parent("gpt-test-old")
    child = _launch(parent)
    assert (child.model, child.provider, child.base_url) == (parent.model, parent.provider, parent.base_url)
    assert child.reasoning_config == parent.reasoning_config
    assert child.fallback_model == parent._fallback_chain
    assert child.request_overrides == parent.request_overrides
    parent.model = "unmatched"
    child = _launch(parent)
    assert child.model == "default-child"
    assert child.reasoning_config == parse_reasoning_effort("high")
    _edit(config, save)
    parent.model = "claude-test-new"
    assert _launch(parent).model == "claude-test-new"


@pytest.mark.parametrize("provider", [None, "openai"])
def test_explicit_lifecycle_route_beats_by_parent(config_file, provider, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-explicit")
    child = _launch(_parent("gpt-test-old"), model="explicit-model", provider=provider)
    assert child.model == "explicit-model"
    assert child.provider == (provider or "custom")


def test_default_backend_and_plugin_options_can_return_live_alias(config_file):
    from agent.delegation_provider import DelegationProvider
    from agent.delegation_registry import _registry
    from tools.delegate_tool_routes import resolve_task_routes
    _home, config, save = config_file

    class Options(DelegationProvider):
        @property
        def name(self):
            return "alias_options_test"

        @property
        def display_name(self):
            return "Alias Options Test"

        def route_task(self, task, config):
            return {"model": self.settings(config)["options"][0]["model"]}

    provider = Options()
    _registry.register(provider)
    try:
        cfg = {"backend": "alias_options_test", "backends": {"alias_options_test": {"options": [{"model": "alias:work"}]}}}
        first, error = resolve_task_routes([{"goal": "Inspect"}], cfg, _parent())
        assert error is None and first[0].error is None
        assert first[0].creds["model"] == "gpt-test-old"
        _edit(config, save)
        second, error = resolve_task_routes([{"goal": "Inspect"}], cfg, _parent())
        assert error is None and second[0].error is None
        assert (second[0].creds["model"], second[0].creds["provider"]) == ("claude-test-new", "anthropic")
        assert cfg["backends"]["alias_options_test"]["options"][0]["model"] == "alias:work"
    finally:
        _registry.restore_registration("alias_options_test", provider, None)
    routes, error = resolve_task_routes([{"goal": "Inspect"}], {"model": "alias:work"}, _parent())
    assert error is None and routes[0].creds["model"] == "claude-test-new"


def test_unknown_alias_refuses_topic_cron_and_child(config_file):
    from hermes_cli.model_switch import switch_model
    from tools.delegate_tool_routes import resolve_task_routes
    _home, config, save = config_file
    config["model_aliases"].clear()
    save()
    result = switch_model("alias:work", "openai", "before")
    assert not result.success and "Unknown live model alias" in result.error_message
    with pytest.raises(ValueError, match="Unknown live model alias"):
        _cron_fire({"model": "alias:work"})
    routes, error = resolve_task_routes([{"goal": "Inspect"}], {"model": "alias:work"}, _parent())
    assert error is None and "Unknown live model alias" in routes[0].error


@pytest.mark.parametrize("routes, message", [
    ({"parent-live": "missing"}, "Unknown live model alias"),
    ({"alias:missing": "inherit"}, "Unknown live model alias"),
    ({"gpt-test-old": "inherit", "alias:work": "inherit"}, "Duplicate"),
    ({"parent-live": {"model": "inline"}}, "alias names or inherit"),
])
def test_by_parent_invalid_maps_fail_loudly(config_file, routes, message):
    from tools.delegate_tool_routes import resolve_task_routes
    cfg = {"backend": "by_parent", "backends": {"by_parent": {"routes": routes}}}
    found, error = resolve_task_routes([{"goal": "Inspect"}], cfg, _parent())
    assert error is None and message in found[0].error


def test_startup_and_frozen_policy_keep_reference_live(config_file):
    from gateway.session_local_route import resolve_launch_route
    from gateway.session_policy import build_policy
    from gateway.session_worker_construct import route_runtime
    from hermes_cli.model_switch import resolve_startup_model_route
    _home, config, save = config_file
    assert resolve_startup_model_route("alias:work").model == "gpt-test-old"
    launch = resolve_launch_route({"source": "cli", "model": "alias:work"}, config)
    assert launch["model"] == "alias:work"
    policy = build_policy(launch, config, profile_terminal=False)
    stored = policy.config_json
    frozen, model = policy.effective()
    runtime, fallback = route_runtime(policy, frozen, model, None)
    assert (model, runtime["provider"], fallback) == ("gpt-test-old", "openai", None)
    _edit(config, save)
    frozen, model = policy.effective()
    runtime, _fallback = route_runtime(policy, frozen, model, None)
    assert (model, runtime["provider"]) == ("claude-test-new", "anthropic")
    assert frozen["_live_alias_reasoning"] == parse_reasoning_effort("high")
    assert policy.config_json == stored and policy.model == "alias:work"
    assert "api_key" not in policy.effective(secrets=False)[0]["model"]


def test_plain_alias_startup_still_copies_and_global_pick_stores_live_marker(config_file):
    from hermes_cli.model_switch import apply_model_selection, resolve_startup_model_route, switch_model
    _home, config, save = config_file
    picked = resolve_startup_model_route("work")
    live = switch_model("alias:work", "openai", "before")
    stored = apply_model_selection(config["model"], live)
    assert stored["default"] == "alias:work"
    _edit(config, save)
    assert picked.model == "gpt-test-old"
    assert resolve_startup_model_route("work").model == "claude-test-new"
    assert not switch_model("alias:work", "openai", "before", explicit_provider="anthropic").success


@pytest.mark.parametrize("model_field", ["default", "cron"])
def test_cron_default_alias_moves_on_next_fire(config_file, model_field):
    _home, config, save = config_file
    if model_field == "cron":
        config["cron"]["model"] = "alias:work"
        config["model"]["default"] = "unrelated-model"
        save()
    first = _cron_fire({})
    _edit(config, save)
    second = _cron_fire({})
    assert (first["model"], first["provider"]) == ("gpt-test-old", "openai")
    assert (second["model"], second["provider"]) == ("claude-test-new", "anthropic")
    assert second["reasoning_config"] == parse_reasoning_effort("high")


def test_gateway_default_and_reasoning_pin(config_file):
    from gateway.run import GatewayRunner
    _home, config, save = config_file
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner._session_db = None
    model, runtime = runner._resolve_session_agent_runtime(session_key="held-default")
    assert (model, runtime["provider"]) == ("gpt-test-old", "openai")
    runner._session_state("held-default").conversation.reasoning_override = parse_reasoning_effort("medium")
    _edit(config, save)
    model, runtime = runner._resolve_session_agent_runtime(session_key="held-default")
    assert (model, runtime["provider"]) == ("claude-test-new", "anthropic")
    assert runner._resolve_session_reasoning_config(session_key="held-default", model=model) == parse_reasoning_effort("medium")


def test_local_policy_explicit_reasoning_beats_alias(config_file):
    from gateway.session_policy import build_policy
    _home, config, save = config_file
    policy = build_policy({"source": "cli", "model": "alias:work", "reasoning": "medium"}, config, profile_terminal=False)
    assert policy.reasoning_config == parse_reasoning_effort("medium")
    _edit(config, save)
    assert policy.reasoning_config == parse_reasoning_effort("medium")


@pytest.mark.parametrize("stale", [False, True])
def test_live_child_honors_only_current_explicit_parent_reasoning(config_file, stale):
    parent = _parent()
    parent.reasoning_override = parse_reasoning_effort("medium")
    if stale:
        parent.reasoning_config = parse_reasoning_effort("high")
    child = _launch(parent)
    assert child.reasoning_config == parse_reasoning_effort("low" if stale else "medium")


def test_nested_inherit_and_unmatched_runtime_parity(config_file):
    _home, config, save = config_file
    parent = _parent("gpt-test-old")
    first = _launch(parent)
    first.session_id = "nested-parent"
    first._fallback_chain = first.fallback_model
    second = _launch(first)
    assert (second.model, second.provider, second.base_url, second.fallback_model) == (
        first.model, first.provider, first.base_url, first.fallback_model)
    parent.model = "unmatched"
    routed = _launch(parent)
    config["delegation"]["backend"] = "default"
    save()
    plain = _launch(parent)
    for field in ("model", "provider", "base_url", "api_key", "api_mode", "reasoning_config", "fallback_model", "request_overrides"):
        assert getattr(routed, field) == getattr(plain, field)
