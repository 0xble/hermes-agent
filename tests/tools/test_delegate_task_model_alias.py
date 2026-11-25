"""Per-task ``model`` (a configured alias) and ``reasoning_effort`` on delegate_task.

The alias list comes from the same loader ``/model`` uses, read when the schema is built. An alias beats the role
``option`` for the model and brings only its own fallback chain. An explicit effort beats every default.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.delegation_provider import DelegationProvider
from tools import delegate_tool_routes as routes
from tools.delegate_tool_config import _child_reasoning

ALIASES = {
    "fable": {"model": "claude-fable-5-2", "provider": "custom:claude-proxy",
              "fallback_providers": [{"provider": "custom:claude-proxy-b", "model": "claude-fable-5-2"}]},
    "astra": {"model": "gpt-6.2-astra", "provider": "custom:openai-proxy", "reasoning_effort": "high"},
}


class _Roles(DelegationProvider):
    @property
    def name(self):
        return "test-roles"

    def task_parameters(self, config):
        return {"option": {"type": "string", "enum": ["worker", "critical"]}}

    def route_task(self, task, config):
        role = task.get("option") or "worker"
        return {"model": f"{role}-model", "provider": "custom:role", "label": role, "reasoning_effort": "low",
                "fallback_providers": [{"provider": "custom:role-b", "model": f"{role}-model"}]}


@pytest.fixture
def cfg(monkeypatch):
    config = {"model_aliases": json.loads(json.dumps(ALIASES))}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr(routes, "_active_provider", lambda _cfg: _Roles())
    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials",
                        lambda route, parent: {"model": route["model"], "provider": route["provider"],
                                               "base_url": None, "api_key": None, "api_mode": None,
                                               "request_overrides": {}})
    return config


def _parent():
    return SimpleNamespace(reasoning_config={"enabled": True, "effort": "medium"}, reasoning_override=None)


def _task_props():
    from tools.delegate_tool import _build_dynamic_schema_overrides
    return _build_dynamic_schema_overrides()["parameters"]["properties"]["tasks"]["items"]["properties"]


def test_schema_offers_configured_aliases_and_efforts_and_tracks_new_aliases(cfg):
    props = _task_props()
    assert props["model"]["enum"] == ["astra", "fable"]
    assert "only when the user explicitly asks" in props["model"]["description"]
    assert props["reasoning_effort"]["enum"] == ["none", "low", "medium", "high", "xhigh"]
    assert props["option"]["enum"] == ["worker", "critical"]  # roles stay offered alongside
    cfg["model_aliases"]["haiku"] = {"model": "claude-haiku-5-5", "provider": "custom:claude-proxy"}
    assert "haiku" in _task_props()["model"]["enum"]


def test_alias_wins_over_role_and_brings_only_its_own_fallbacks(cfg):
    (route,), err = routes.resolve_task_routes([{"goal": "g", "model": "Fable", "option": "critical"}], {}, _parent())
    assert err is None and route.error is None
    assert (route.creds["model"], route.creds["provider"]) == ("claude-fable-5-2", "custom:claude-proxy")
    assert route.routing_cfg["fallback_providers"] == ALIASES["fable"]["fallback_providers"]
    assert route.routing_cfg["_model_pinned"] is True
    assert route.report() == {"backend": "model_alias", "label": "fable"}


def test_alias_fallback_chain_filters_other_models(cfg):
    cfg["model_aliases"]["fable"]["fallback_providers"] = [
        {"provider": "custom:claude-proxy-b", "model": "claude-fable-5-2"},
        {"provider": "custom:other", "model": "other-model"},
    ]
    (route,), err = routes.resolve_task_routes([{"goal": "g", "model": "fable"}], {}, _parent())
    assert err is None and route.error is None
    assert route.routing_cfg["fallback_providers"] == [
        {"provider": "custom:claude-proxy-b", "model": "claude-fable-5-2"}
    ]


def test_alias_without_a_chain_never_borrows_the_delegation_chain(cfg):
    (route,), _ = routes.resolve_task_routes([{"goal": "g", "model": "astra"}],
                                             {"fallback_providers": [{"provider": "x", "model": "y"}]}, _parent())
    assert route.routing_cfg["fallback_providers"] == []
    assert route.reasoning_effort == "high"  # the alias's own default effort
    assert (route.creds["model"], route.creds["provider"]) == ("gpt-6.2-astra", "custom:openai-proxy")


def test_model_section_alias_keeps_its_own_fallback_chain_and_effort(cfg):
    chain = [{"provider": "custom:b", "model": "m-1"}]
    cfg["model"] = {"provider": "custom:a", "aliases": {
        "quick": {"model": "m-1", "fallback_providers": chain, "reasoning_effort": "high"}}}
    assert "quick" in _task_props()["model"]["enum"]
    (route,), err = routes.resolve_task_routes([{"goal": "g", "model": "quick"}], {}, _parent())
    assert err is None and route.error is None
    assert (route.creds["model"], route.creds["provider"]) == ("m-1", "custom:a")
    assert route.routing_cfg["fallback_providers"] == chain
    assert _child_reasoning(_parent(), route.reasoning_cfg or {}, route.reasoning_effort) == {
        "enabled": True, "effort": "high"}


@pytest.mark.parametrize("order", [("role", "alias"), ("alias", "role")])
def test_alias_and_role_on_one_route_keep_their_own_backend(cfg, monkeypatch, order):
    """A role whose route equals an explicit alias's must not share its cached backend across a batch."""
    monkeypatch.setattr(_Roles, "route_task",
                        lambda self, task, config: {"model": "astra", "label": "astra", "fallback_providers": []})
    tasks = {"role": {"goal": "r", "option": "worker"}, "alias": {"goal": "a", "model": "astra"}}
    result, err = routes.resolve_task_routes([tasks[o] for o in order], {}, _parent())
    assert err is None
    backends = {o: (r.report() or {}).get("backend") for o, r in zip(order, result)}
    assert backends == {"role": "test-roles", "alias": "model_alias"}


def test_explicit_effort_beats_alias_role_and_parent_defaults(cfg):
    parent = SimpleNamespace(reasoning_config={"enabled": True, "effort": "xhigh"},
                             reasoning_override={"enabled": True, "effort": "xhigh"})
    tasks = [{"goal": "a", "model": "astra", "reasoning_effort": "medium"},
             {"goal": "b", "option": "critical", "reasoning_effort": "none"},
             {"goal": "c", "option": "critical"}]
    result, err = routes.resolve_task_routes(tasks, {"reasoning_effort": "low"}, parent)
    assert err is None
    efforts = [_child_reasoning(parent, r.reasoning_cfg or {}, r.reasoning_effort) for r in result]
    assert efforts[0] == {"enabled": True, "effort": "medium"}
    assert efforts[1] == {"enabled": False}
    assert efforts[2] == {"enabled": True, "effort": "xhigh"}  # unset: existing precedence is unchanged
    assert result[2].creds["model"] == "critical-model"


def test_unavailable_backend_never_blocks_an_explicit_alias(cfg, monkeypatch):
    """The backend is consulted only for tasks without `model`: when it cannot run, alias tasks still route,
    role tasks are refused alone, and an all-role batch fails closed as before."""
    def unavailable(_cfg):
        raise routes.DelegationRouteError("delegation backend 'gone' is not installed")

    monkeypatch.setattr(routes, "_active_provider", unavailable)
    (alias, role), err = routes.resolve_task_routes([{"goal": "a", "model": "fable"}, {"goal": "r"}], {}, _parent())
    assert err is None and alias.error is None and alias.creds["model"] == "claude-fable-5-2"
    assert role.error and "not installed" in role.error
    assert routes.resolve_task_routes([{"goal": "r"}], {}, _parent()) == ([], "delegation backend 'gone' is not installed")


def test_keyed_alias_without_a_url_keeps_its_own_key(cfg, monkeypatch):
    """A keyed alias with no base_url routes by its provider label with its own key, never a re-resolved or
    inherited one, through to the runtime provider resolver."""
    cfg["model_aliases"]["keyed"] = {"model": "m-k", "provider": "openrouter", "api_key": "alias-key"}
    seen = []
    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials",
                        lambda route, parent: seen.append(dict(route)) or {"model": route["model"],
                                                                            "provider": route["provider"]})
    (route,), err = routes.resolve_task_routes([{"goal": "g", "model": "keyed"}], {}, _parent())
    assert err is None and route.error is None
    assert (seen[0]["provider"], seen[0]["api_key"]) == ("openrouter", "alias-key") and "base_url" not in seen[0]

    from tools import delegate_tool_config
    calls = []
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider",
                        lambda **kw: calls.append(kw) or {"api_key": kw.get("explicit_api_key") or "env-key",
                                                          "base_url": "https://openrouter.ai/api/v1",
                                                          "provider": "openrouter"})
    creds = delegate_tool_config._resolve_delegation_credentials(
        {"model": "m-k", "provider": "openrouter", "api_key": "alias-key"}, _parent())
    assert calls[0]["explicit_api_key"] == "alias-key" and creds["api_key"] == "alias-key"


def test_endpoint_alias_without_a_key_cannot_inherit_parent_key(cfg, monkeypatch):
    cfg["model_aliases"]["remote"] = {"model": "m-remote", "provider": "custom:remote",
                                         "base_url": "https://remote.example/v1"}
    captured = []
    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials",
                        lambda route, parent: captured.append(dict(route)) or {
                            "model": route["model"], "provider": route["provider"], "base_url": route["base_url"],
                            "api_key": None, "api_mode": "chat_completions", "request_overrides": {},
                            "no_parent_api_key": route.get("_no_parent_api_key", False),
                        })
    (route,), err = routes.resolve_task_routes([{"goal": "g", "model": "remote"}], {}, _parent())
    assert err is None and route.error is None
    assert captured[0]["_no_parent_api_key"] is True
    from tools.delegate_tool import _credential_overrides
    overrides = _credential_overrides(route.creds, route.routing_cfg)
    assert overrides["prevent_parent_api_key"] is True

    from tools.delegate_tool_config import _resolve_child_runtime
    parent = SimpleNamespace(model="parent", provider="custom:parent", base_url="https://parent.example/v1",
                             api_mode="chat_completions", api_key="parent-secret", acp_command=None,
                             acp_args=[], reasoning_config={"enabled": True, "effort": "medium"},
                             requested_provider="custom:parent")
    runtime = _resolve_child_runtime(parent, {}, "parent-secret", model="m-remote",
                                     override_provider="custom:remote", override_base_url="https://remote.example/v1",
                                     override_api_key=None, prevent_parent_api_key=True, override_api_mode="chat_completions",
                                     override_acp_command=None, override_acp_args=[], routing_cfg={})
    assert runtime["api_key"] is None


def test_unset_model_keeps_the_role_route(cfg):
    (route,), _ = routes.resolve_task_routes([{"goal": "g", "option": "critical"}], {}, _parent())
    assert route.creds["model"] == "critical-model" and route.report() == {"backend": "test-roles",
                                                                          "label": "critical"}


@pytest.mark.parametrize("task, needle", [
    ({"model": "gpt-9"}, "unknown model alias 'gpt-9'. Use one of: astra, fable"),
    ({"model": "astra", "reasoning_effort": "max"}, "'reasoning_effort' must be one of none, low, medium"),
])
def test_bad_alias_or_effort_refuses_that_task_alone(cfg, task, needle):
    bad, good = routes.resolve_task_routes([{"goal": "bad", **task}, {"goal": "good"}], {}, _parent())[0]
    assert needle in bad.error and good.error is None


def test_real_spawn_hands_the_child_the_alias_model_and_effort(cfg):
    """Through real delegate_task: the built child gets the alias route, pin and explicit effort."""
    built = []

    def construct(**kwargs):
        built.append(kwargs)
        return SimpleNamespace(**{k: kwargs.get(k) for k in ("model", "provider", "reasoning_config")},
                               request_overrides={}, service_tier=None)

    parent = SimpleNamespace(reasoning_config={"enabled": True, "effort": "high"}, reasoning_override=None)
    with (
        patch("tools.delegate_tool._load_config", return_value={}),
        patch("tools.delegate_tool._build_child_preserving_parent_tools",
              side_effect=lambda **kw: construct(model=kw["model"], provider=kw["override_provider"],
                                                 reasoning_config=_child_reasoning(
                                                     parent, kw["reasoning_cfg"], kw["override_reasoning_effort"]))),
        patch("tools.delegate_tool.pin_child_service_tier"),
        patch("tools.delegate_tool._run_batch", side_effect=lambda batch, background: json.dumps({"ok": True})),
        patch("tools.delegate_tool._oneshot_spawn_budget", return_value=None),
    ):
        from tools.delegate_tool import delegate_task
        out = json.loads(delegate_task(tasks=[{"goal": "write the thing", "model": "fable",
                                               "reasoning_effort": "low"}], parent_agent=parent))
    assert out == {"ok": True}
    assert built[0]["model"] == "claude-fable-5-2" and built[0]["provider"] == "custom:claude-proxy"
    assert built[0]["reasoning_config"] == {"enabled": True, "effort": "low"}
