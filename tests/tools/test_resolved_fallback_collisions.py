"""Different selectors must not freeze ambiguous provider/model fallback identities."""
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli.config import load_config
from hermes_cli.runtime_provider import resolve_runtime_provider
from tools.custom_subagents import FallbackDefinition, _freeze_fallback_runtime
from tools.delegate_tool import _preflight_task_runtime


@pytest.fixture
def routes_home(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    token = set_hermes_home_override(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("source", ["role", "parent", "parent_pin"])
@pytest.mark.parametrize("collision", ["primary", "fallback", "none"])
def test_preflight_checks_resolved_fallback_identities(routes_home, source, collision):
    primary_model = "shared" if collision == "primary" else "primary"
    entries = [{"provider": "custom:first", "model": "shared"},
               {"provider": "custom:second", "model": "other" if collision == "none" else "shared"}]
    role = {"description": "Fixture", "instructions": "Fixture"}
    if source == "role":
        role.update(provider="custom:primary", model=primary_model, fallbacks=entries)
    else:
        role["inherit_parent"] = True
    raw = {"providers": {name: {"base_url": f"https://{name}.invalid/v1", "api_key": f"fixture-{name}-key"}
                         for name in ("primary", "first", "second")},
           "delegation": {"subagents": {"fixture": role}}}
    (routes_home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    parent = SimpleNamespace(provider="custom", model=primary_model, base_url="https://primary.invalid/v1",
                             api_mode="chat_completions", api_key="fixture-primary-key",
                             _fallback_chain=entries, _fallback_index=0, reasoning_config=None,
                             request_overrides={}, _client_kwargs={})
    if source == "parent_pin":
        # Simulate already-frozen legacy authority, retaining the actual resolver's
        # provider/model/URL/credential instead of fabricating resolved aliases.
        frozen = tuple(_freeze_fallback_runtime(
            resolve_runtime_provider(requested=e["provider"], target_model=e["model"]),
            FallbackDefinition(**e), "fixture") for e in entries)
        parent._delegation_runtime_pin = SimpleNamespace(fallback_routes=frozen)
        parent._fallback_chain = [r.native_entry() for r in frozen]
    launches, error = _preflight_task_runtime(
        [{"goal": "Fixture", "task_label": "Fixture", "subagent_type": "fixture"}],
        load_config()["delegation"], None, parent, None)
    if collision != "none":
        assert launches == []
        assert "provider/model identity" in error
    else:
        assert error is None
        routes = launches[0].fallback_routes
        assert [(r.provider, r.model, r.base_url) for r in routes] == [
            ("custom", "shared", "https://first.invalid/v1"),
            ("custom", "other", "https://second.invalid/v1")]
        assert [r.api_key for r in routes] == ["fixture-first-key", "fixture-second-key"]
