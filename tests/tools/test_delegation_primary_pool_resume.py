"""Primary resume binds persisted custom selector, endpoint and stable account."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest
import yaml

from hermes_state import SessionDB
from tools.custom_subagents import parse_definitions, resolve_named_credentials
from tools.delegate_tool import _resolve_resume_launch
from tools.delegate_tool_config import _resolve_child_credential_pool


@pytest.mark.parametrize("mutation", [None, "refresh", "wrong_selector_same_endpoint",
    "wrong_selector_other_endpoint", "endpoint", "foreign_account", "missing_id", "overrides"])
def test_primary_custom_pool_resume_preserves_authority(tmp_path, monkeypatch, mutation):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Keep real config and endpoint-bound inherited pool resolution. The pool
    # entries model stable accounts without touching a credential store.
    monkeypatch.setattr("hermes_cli.runtime_provider._try_resolve_from_custom_pool", lambda *a, **kw: None)
    url, key, selector = "https://first.invalid/v1", "synthetic-original", "custom:first"
    config: dict = {"custom_providers": [
        {"name": "first", "base_url": url, "api_key": key, "model": "m"},
        {"name": "second", "base_url": url, "api_key": "synthetic-other", "model": "m"},
        {"name": "third", "base_url": "https://third.invalid/v1", "api_key": "synthetic-third", "model": "m"},
    ]}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    cfg = {"subagents": {"advisor": {"description": "Advise", "instructions": "Analyze",
        "provider": selector, "model": "m"}}}
    definitions = parse_definitions(cfg)
    entry = SimpleNamespace(id="stable-account", provider=selector, runtime_api_key=key, runtime_base_url=url)
    pool = SimpleNamespace(provider=selector, entries=lambda: [entry])
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="cli")
    parent = SimpleNamespace(session_id="root", _session_db=db, provider="custom", base_url=url, _credential_pool=pool)
    assert _resolve_child_credential_pool("custom", parent, url) is pool
    creds, _ = resolve_named_credentials(definitions["advisor"], cfg, parent)
    metadata = {"version": 1, "subagent_type": "advisor", "parent_session_root": "root",
        "provider": "custom", "requested_provider": selector, "model": "m",
        "base_url": url, "api_mode": creds["api_mode"],
        "authority_fingerprint": hashlib.sha256(key.encode()).hexdigest(),
        "credential_pool_entry_id": entry.id, "request_overrides": {}, "fallbacks": []}
    db.create_session("child", source="tool", model_config={"_delegate_from": "root",
        "_delegation_launch": metadata, "_delegation_completed": True})
    db.close()
    db = parent._session_db = SessionDB(tmp_path / "state.db")
    if mutation == "refresh":
        entry.runtime_api_key = "synthetic-refreshed"
    elif mutation in ("wrong_selector_same_endpoint", "wrong_selector_other_endpoint"):
        entry.provider = "custom:second" if mutation == "wrong_selector_same_endpoint" else "custom:third"
        if mutation == "wrong_selector_other_endpoint":
            entry.runtime_base_url = "https://third.invalid/v1"
    elif mutation == "endpoint":
        entry.runtime_base_url = "https://first.invalid/foreign-tenant/v1"
    elif mutation in ("foreign_account", "missing_id"):
        entry.id = "foreign-account" if mutation == "foreign_account" else None
    elif mutation == "overrides":
        config = deepcopy(config)
        config["custom_providers"][0]["extra_body"] = {"fixture_option": "changed"}
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    try:
        if mutation not in (None, "refresh"):
            with pytest.raises(ValueError):
                _resolve_resume_launch({"resume_session_id": "child"}, definitions, parent, defaults=cfg)
        else:
            resumed = _resolve_resume_launch({"resume_session_id": "child"}, definitions, parent, defaults=cfg)
            assert resumed.credentials["api_key"] == entry.runtime_api_key
            assert resumed.credentials["base_url"] == url
            assert resumed.credentials["provider"] == "custom"
            assert resumed.definition.provider == selector
            assert resumed.launch_metadata == metadata
        row = db.get_session("child")
        assert row is not None
        stored = json.loads(row["model_config"])
        assert stored["_delegation_launch"] == metadata
    finally:
        db.close()
