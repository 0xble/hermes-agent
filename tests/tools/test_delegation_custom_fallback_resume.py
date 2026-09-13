"""Real config → frozen fallback → reopened SessionDB → exact resume authority."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest
import yaml

from hermes_state import SessionDB
from tools.custom_subagents import freeze_fallback_routes, parse_definitions, resolve_named_credentials
from tools.delegate_tool import _resolve_resume_launch


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("mutation,pooled", [(None, False), (None, True),
    ("base_url", False), ("api_key", False), ("extra_body", False), ("legacy", False)])
def test_custom_fallback_resume_keeps_selector_and_physical_authority(tmp_path, monkeypatch, active, mutation, pooled):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if not pooled:
        # Static credentials have no stable account refresh authority. Keep real
        # config/provider resolution; isolate only the optional account pool.
        monkeypatch.setattr("hermes_cli.runtime_provider._try_resolve_from_custom_pool", lambda *a, **kw: None)
    config = {"providers": {name: {"base_url": f"https://{name}.invalid/v1",
        "api_key": f"sentinel-{name}", "default_model": f"{name}-model"} for name in ("primary", "first")}}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    cfg = {"subagents": {"advisor": {"description": "Advise", "instructions": "Analyze",
        "provider": "custom:primary", "model": "primary-model",
        "fallbacks": [{"provider": "custom:first", "model": "first-model"}]}}}
    definitions = parse_definitions(cfg)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="cli")
    parent = SimpleNamespace(session_id="root", _session_db=db)
    creds, _ = resolve_named_credentials(definitions["advisor"], cfg, parent)
    routes = freeze_fallback_routes(definitions["advisor"], primary_provider="custom", primary_model="primary-model")
    metadata = {"version": 1, "subagent_type": "advisor", "parent_session_root": "root",
        "provider": "custom", "requested_provider": "custom:primary", "model": "primary-model",
        "base_url": creds["base_url"], "api_mode": creds["api_mode"],
        "authority_fingerprint": hashlib.sha256(creds["api_key"].encode()).hexdigest(),
        "request_overrides": {}, "fallback_source": "role", "fallbacks": [r.metadata() for r in routes]}
    if mutation == "legacy":
        metadata["fallbacks"][0].pop("requested_provider", None)
    db.create_session("child", source="tool", model_config={"_delegate_from": "root",
        "_delegation_launch": metadata, "_delegation_completed": True,
        **({"_delegation_active_route": {"provider": "custom", "model": "first-model"}} if active else {})})
    db.close()
    db = parent._session_db = SessionDB(tmp_path / "state.db")
    changed = deepcopy(config)
    if mutation in ("base_url", "api_key", "extra_body"):
        changed["providers"]["first"][mutation] = {
            "base_url": "https://other.invalid/v1", "api_key": "changed-sentinel",
            "extra_body": {"fixture_option": "changed"},
        }[mutation]
        config_path.write_text(yaml.safe_dump(changed), encoding="utf-8")
    try:
        if mutation:
            with pytest.raises(ValueError):
                _resolve_resume_launch({"resume_session_id": "child"}, definitions, parent, defaults=cfg)
        else:
            resumed = _resolve_resume_launch({"resume_session_id": "child"}, definitions, parent, defaults=cfg)
            assert metadata["fallbacks"][0]["requested_provider"] == "custom:first"
            route = resumed.credentials if active else resumed.fallback_routes[0].native_entry()
            assert route["base_url"] == config["providers"]["first"]["base_url"]
            assert route["api_key"] == config["providers"]["first"]["api_key"]
            assert resumed.launch_metadata == metadata
            assert bool(resumed.fallback_routes) is not active
        row = db.get_session("child")
        assert row is not None
        stored = row["model_config"]
        assert "sentinel-" not in stored
        assert json.loads(stored)["_delegation_launch"] == metadata
    finally:
        db.close()
