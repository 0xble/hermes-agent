"""Settings › Subagents routes: ``delegation.backend`` and ``delegation.backends.*`` are read, validated and
written in the TARGET profile (``?profile=``). Real plugin directory registering real delegation providers, the
real loader and real config files under a temp home.
"""

from __future__ import annotations

import hermes_yaml as yaml
import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient

ROUTES_PLUGIN = '''
from agent.delegation_provider import DelegationProvider


class _Routes(DelegationProvider):
    def __init__(self, name, route_keys=()):
        self._name = name
        self._route_keys = route_keys

    @property
    def name(self):
        return self._name

    @property
    def route_keys(self):
        return self._route_keys

    def route_task(self, task, config):
        return dict(config)


def register(ctx):
    ctx.register_delegation_provider(_Routes("model_map", ("daybreak", "", 3, "daybreak")))
    ctx.register_delegation_provider(_Routes("profile_routes"))
'''

SECRET = "sk-route-secret-0123456789abcdef"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    from hermes_cli import plugins as plugins_mod

    root = tmp_path / "hermes_home"
    work = root / "profiles" / "work"
    plugin_dir = work / "plugins" / "routes"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump({"name": "routes", "version": "1.0"}), encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(ROUTES_PLUGIN, encoding="utf-8")
    (work / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["routes"]},
        "delegation": {"backend": "model_map", "backends": {"model_map": {"options": [
            {"name": "fast", "description": "Quick", "provider": "openrouter", "model": "m-fast", "api_key": SECRET,
             "request_overrides": {"extra_body": {"seed": 7}}},
            {"name": "deep", "description": "Careful", "provider": "anthropic", "model": "m-deep"},
        ], "default": "deep"}}},
    }), encoding="utf-8")
    # The launch profile saved a plugin backend it does not have: configured, but nothing can run it.
    (root / "config.yaml").write_text(yaml.safe_dump({"delegation": {"backend": "model_map"}}), encoding="utf-8")
    empty_bundled = tmp_path / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    from agent import secret_scope
    from tui_gateway import launch_profile_policy as lpp
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setattr(lpp, "_snapshot", None)
    plugins_mod._reset_plugin_managers_for_tests()
    yield root, work
    plugins_mod._reset_plugin_managers_for_tests()


@pytest.fixture
def client(homes):
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def _delegation(home) -> dict:
    return (yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}).get("delegation") or {}


def test_routing_reads_the_target_profile_and_never_returns_a_route_key(client):
    work = client.get("/api/delegation/routing?profile=work").json()
    assert {b["name"] for b in work["backends"]} == {"default", "model_map", "profile_routes"}
    assert (work["configured"], work["blocked_reason"]) == ("model_map", None)
    assert [o["name"] for o in work["model_map"]["options"]] == ["fast", "deep"]
    assert work["model_map"]["default"] == "deep"
    assert SECRET not in str(work) and work["model_map"]["options"][0]["api_key"]
    assert {"default", "work"} <= set(work["profiles"])

    # Core fails closed on a saved backend no plugin provides: the page must say why, not show Default.
    launch = client.get("/api/delegation/routing").json()
    assert [b["name"] for b in launch["backends"]] == ["default"]
    assert launch["configured"] == "model_map" and "not installed" in launch["blocked_reason"]


def test_a_backend_can_only_be_chosen_where_its_plugin_is_registered(client, homes):
    root, work = homes
    resp = client.put("/api/delegation/backend?profile=work", json={"backend": "profile_routes"})
    assert resp.status_code == 200, resp.text
    assert _delegation(work)["backend"] == "profile_routes"

    before = (root / "config.yaml").read_text(encoding="utf-8")
    resp = client.put("/api/delegation/backend", json={"backend": "profile_routes"})
    assert resp.status_code == 400
    assert "not installed" in resp.json()["detail"]
    assert (root / "config.yaml").read_text(encoding="utf-8") == before


@pytest.mark.parametrize(("path", "body"), [
    ("/api/delegation/backend", {"backend": "default"}),
    ("/api/delegation/backends/model_map", {"options": []}),
    ("/api/delegation/backends/profile_routes", {"profiles": []}),
])
def test_a_managed_install_refuses_every_routing_write_with_the_reason(client, homes, monkeypatch, path, body):
    """config.yaml of a package-manager-managed install is not ours: each write is a 409 naming the manager,
    never a silent ``ok`` over an untouched file."""
    _root, work = homes
    before = (work / "config.yaml").read_text(encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED", "nixos")
    resp = client.put(f"{path}?profile=work", json=body)
    assert resp.status_code == 409, resp.text
    assert "managed by nixos" in resp.json()["detail"] and "\n" not in resp.json()["detail"]
    assert (work / "config.yaml").read_text(encoding="utf-8") == before


@pytest.mark.parametrize(("options", "problem"), [
    ([{"name": "a", "description": "x", "provider": "p", "model": "m"},
      {"name": "a", "description": "y", "provider": "p", "model": "m"}], "unique"),
    ([{"name": "a", "provider": "p", "model": "m"}], "description"),
    ([{"name": "a", "description": "x", "model": "m"}], "provider"),
    ([{"name": "a", "description": "x", "provider": "p", "model": "m", "service_tier": "warp"}], "service tier"),
    ([{"name": "a", "description": "x", "provider": "p", "model": "m", "reasoning_effort": "huge"}], "reasoning"),
])
def test_invalid_model_routes_are_refused_before_anything_is_written(client, homes, options, problem):
    _root, work = homes
    before = (work / "config.yaml").read_text(encoding="utf-8")
    resp = client.put("/api/delegation/backends/model_map?profile=work", json={"options": options})
    assert resp.status_code == 400
    assert problem in resp.json()["detail"]
    assert (work / "config.yaml").read_text(encoding="utf-8") == before


@pytest.mark.parametrize(("chain", "problem"), [
    ([{"provider": "openai", "model": "gpt-6-sol"}], "at least 3 fallback entries"),
    ([{"provider": "custom:openai-proxy", "model": f"gpt-{n}"} for n in "abc"], "upstream families"),
    ([{"provider": "openai", "model": "gpt-6-sol"}, {"provider": "anthropic", "model": "claude-opus-5-5"},
      {"provider": "xai-oauth", "model": "grok-4.7"}], "duplicates the primary route"),
])
def test_model_route_chains_below_the_resilience_bar_are_refused_before_anything_is_written(client, homes, chain, problem):
    _root, work = homes
    before = (work / "config.yaml").read_text(encoding="utf-8")
    route = {"name": "deep", "description": "Careful", "provider": "openai", "model": "gpt-6-sol",
             "fallback_providers": chain}
    resp = client.put("/api/delegation/backends/model_map?profile=work", json={"options": [route]})
    assert resp.status_code == 400
    assert problem in resp.json()["detail"]
    assert (work / "config.yaml").read_text(encoding="utf-8") == before


def test_routes_without_a_chain_save_beside_one_that_declares_a_chain(client, homes):
    """Only a route that declares a chain is held to the resilience bar. One with no chain, or an
    explicit empty one, saves as before; a weak chain on a sibling still refuses the whole save."""
    _root, work = homes
    chain = [{"provider": "anthropic", "model": "claude-opus-5-5"},
             {"provider": "custom:openai-proxy", "model": "muse-wonder-full-1.0"},
             {"provider": "xai-oauth", "model": "grok-4.7"}]
    base = {"description": "x", "provider": "openai", "model": "gpt-6-sol"}
    options = [{**base, "name": "plain"}, {**base, "name": "opted-out", "fallback_providers": []},
               {**base, "name": "chained", "fallback_providers": chain}]
    resp = client.put("/api/delegation/backends/model_map?profile=work", json={"options": options})
    assert resp.status_code == 200, resp.text
    saved = {o["name"]: o for o in _delegation(work)["backends"]["model_map"]["options"]}
    assert "fallback_providers" not in saved["plain"]
    assert saved["opted-out"]["fallback_providers"] == []
    assert saved["chained"]["fallback_providers"] == chain

    before = (work / "config.yaml").read_text(encoding="utf-8")
    options.append({**base, "name": "weak", "fallback_providers": chain[:1]})
    resp = client.put("/api/delegation/backends/model_map?profile=work", json={"options": options})
    assert resp.status_code == 400
    assert "options[3].fallback_providers needs at least 3 fallback entries" in resp.json()["detail"]
    assert (work / "config.yaml").read_text(encoding="utf-8") == before


_CLAUDE = {"provider": "custom:claude-proxy", "model": "claude-sonnet-5-5"}
_GROK = {"provider": "xai-oauth", "model": "grok-4.7"}


@pytest.mark.parametrize(("chain", "problem"), [
    # The alias's real route, written out: a duplicate of the primary.
    ([{"provider": "custom:openai-proxy", "model": "gpt-6.1-sol"}, _CLAUDE, _GROK], "duplicates the primary route"),
    # GPT primary, GPT and Claude backups: two real families, whatever the alias is called.
    ([{"provider": "custom:openai-proxy", "model": "gpt-6.1-terra"}, _CLAUDE,
      {"provider": "custom:claude-proxy", "model": "claude-haiku-5-5"}], "spans only 2 upstream families"),
    ([_CLAUDE, {"provider": "custom:openai-proxy", "model": "muse-wonder-full-1.0"}, _GROK], None),
])
def test_an_alias_named_route_is_expanded_before_its_chain_is_checked(client, homes, chain, problem):
    """A model-map route names a ``model_aliases`` entry (the migrated shape). The alias lives at the
    config root, so the save has to judge the chain against the whole config, not the block."""
    _root, work = homes
    path = work / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["model_aliases"] = {"coder": {"provider": "custom:openai-proxy", "model": "gpt-6.1-sol"}}
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    route = {"name": "coder", "description": "Builds", "provider": "custom:openai-proxy", "model": "coder",
             "fallback_providers": chain}
    resp = client.put("/api/delegation/backends/model_map?profile=work", json={"options": [route]})
    if problem is None:
        assert resp.status_code == 200, resp.text
        assert _delegation(work)["backends"]["model_map"]["options"][0]["model"] == "coder"
    else:
        assert resp.status_code == 400, resp.text
        assert problem in resp.json()["detail"]
        assert path.read_text(encoding="utf-8") == before


def test_saving_model_routes_replaces_the_block_and_keeps_the_stored_key(client, homes):
    root, work = homes
    shown = client.get("/api/delegation/routing?profile=work").json()["model_map"]["options"][0]
    # Rename and edit "fast" as the renderer would (preview key and untouched fields sent back, the old name in
    # original_name), delete "deep", add "local".
    body = {"options": [{**shown, "name": "speedy", "original_name": "fast", "model": "m-faster",
                         "reasoning_effort": "low"},
                        {"name": "local", "description": "Private", "base_url": "http://127.0.0.1:8080/v1",
                         "model": "qwen", "service_tier": "fast"}],
            "default": "local"}
    resp = client.put("/api/delegation/backends/model_map?profile=work", json=body)
    assert resp.status_code == 200, resp.text
    assert SECRET not in resp.text

    block = _delegation(work)["backends"]["model_map"]
    assert block["default"] == "local"
    assert [o["name"] for o in block["options"]] == ["speedy", "local"]
    assert block["options"][0] == {"name": "speedy", "description": "Quick", "provider": "openrouter",
                                   "model": "m-faster", "reasoning_effort": "low", "api_key": SECRET,
                                   "request_overrides": {"extra_body": {"seed": 7}}}
    assert "backends" not in _delegation(root)


def test_routing_lists_each_backends_declared_route_keys(client):
    backends = {b["name"]: b["route_keys"] for b in client.get("/api/delegation/routing?profile=work").json()["backends"]}
    # Blank, non-string and repeated declarations are dropped; a backend that declares nothing lists nothing.
    assert backends == {"default": [], "model_map": ["daybreak"], "profile_routes": []}


def test_a_declared_route_key_round_trips_and_removing_it_turns_it_off(client, homes):
    _root, work = homes
    route = {"name": "daybreak", "description": "Sol, reduced guardrails", "provider": "openai-codex",
             "model": "gpt-6-sol", "service_tier": "fast"}
    resp = client.put("/api/delegation/backends/model_map?profile=work", json={"options": [{**route, "daybreak": True}]})
    assert resp.status_code == 200, resp.text
    assert _delegation(work)["backends"]["model_map"]["options"][0]["daybreak"] is True
    # Re-saving what the renderer was shown keeps it (an unrelated edit must never silently drop it).
    shown = client.get("/api/delegation/routing?profile=work").json()["model_map"]["options"]
    resp = client.put("/api/delegation/backends/model_map?profile=work",
                      json={"options": [{**shown[0], "original_name": "daybreak", "reasoning_effort": "high"}]})
    assert resp.status_code == 200, resp.text
    assert _delegation(work)["backends"]["model_map"]["options"][0]["daybreak"] is True
    # The editor turns it off by leaving the key out.
    assert client.put("/api/delegation/backends/model_map?profile=work", json={"options": [route]}).status_code == 200
    assert "daybreak" not in _delegation(work)["backends"]["model_map"]["options"][0]


def test_an_undeclared_key_survives_only_an_untouched_round_trip(client, homes):
    _root, work = homes
    stored = {"name": "deep", "description": "Careful", "provider": "anthropic", "model": "m-deep", "legacy": {"a": 1}}
    assert client.put("/api/delegation/backends/model_map?profile=work", json={"options": [stored]}).status_code == 200
    # A key nobody stored (no backend declares it) is dropped, however the renderer sends it.
    assert "legacy" not in _delegation(work)["backends"]["model_map"]["options"][0]

    block = _delegation(work)
    block["backends"]["model_map"]["options"][0]["legacy"] = {"a": 1}  # hand-written in config.yaml
    (work / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["routes"]}, "delegation": block}),
                                      encoding="utf-8")
    shown = client.get("/api/delegation/routing?profile=work").json()["model_map"]["options"][0]
    resp = client.put("/api/delegation/backends/model_map?profile=work",
                      json={"options": [{**shown, "original_name": "deep", "model": "m-deeper"}]})
    assert resp.status_code == 200, resp.text
    assert _delegation(work)["backends"]["model_map"]["options"][0]["legacy"] == {"a": 1}
    changed = {**shown, "legacy": {"a": 2}}
    assert client.put("/api/delegation/backends/model_map?profile=work", json={"options": [changed]}).status_code == 200
    assert "legacy" not in _delegation(work)["backends"]["model_map"]["options"][0]


def test_a_null_extra_key_is_a_value_and_leaving_it_out_removes_it(client, homes):
    _root, work = homes
    route = {"name": "deep", "description": "Careful", "provider": "anthropic", "model": "m-deep"}

    def save(**extra):
        resp = client.put("/api/delegation/backends/model_map?profile=work", json={"options": [{**route, **extra}]})
        assert resp.status_code == 200, resp.text
        return _delegation(work)["backends"]["model_map"]["options"][0]

    # A declared key sent as null is kept as sent; leaving it out removes it.
    assert save(daybreak=None) == {**route, "daybreak": None}
    assert "daybreak" not in save()

    # A stored null in an undeclared key survives a save that sends it back, and goes when the save leaves it out.
    block = _delegation(work)
    block["backends"]["model_map"]["options"][0]["legacy"] = None  # hand-written in config.yaml
    (work / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["routes"]}, "delegation": block}),
                                      encoding="utf-8")
    assert save(legacy=None) == {**route, "legacy": None}
    assert "legacy" not in save()


def test_profile_routes_only_name_existing_profiles(client, homes):
    _root, work = homes
    before = (work / "config.yaml").read_text(encoding="utf-8")
    resp = client.put("/api/delegation/backends/profile_routes?profile=work",
                      json={"profiles": [{"name": "ghost"}]})
    assert resp.status_code == 400 and "does not exist" in resp.json()["detail"]
    assert (work / "config.yaml").read_text(encoding="utf-8") == before

    resp = client.put("/api/delegation/backends/profile_routes?profile=work",
                      json={"profiles": [{"name": "default", "description": "  The main one  "}], "default": "default"})
    assert resp.status_code == 200, resp.text
    assert _delegation(work)["backends"]["profile_routes"] == {
        "profiles": [{"name": "default", "description": "The main one"}], "default": "default"}
