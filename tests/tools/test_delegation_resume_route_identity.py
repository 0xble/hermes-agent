"""Custom route resume preserves physical authority (offline)."""
import hashlib
import json
from types import SimpleNamespace
import pytest
import yaml

@pytest.mark.parametrize("historical", [True, False])
def test_custom_selector_resume(tmp_path, monkeypatch, historical):
    from tools.custom_subagents import parse_definitions
    from tools.delegate_tool import _resolve_resume_launch
    selector, model, url, key = "custom:codex-proxy", "fixture-model", "http://127.0.0.1:9876/v1", "fixture-key"
    config = {"custom_providers": [{"name": "Codex Proxy", "base_url": url, "api_key": key, "model": model}]}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    definitions = parse_definitions({"subagents": {"advisor": {"description": "Advise", "instructions": "Analyze", "provider": selector, "model": model}}})
    launch = {"version": 1, "subagent_type": "advisor", "parent_session_root": "root", "provider": selector if historical else "custom", "model": model, "base_url": url, "api_mode": "chat_completions", "authority_fingerprint": hashlib.sha256(key.encode()).hexdigest(), "request_overrides": {}, "fallbacks": []}
    if not historical:
        launch["requested_provider"] = selector
    state = {"_delegation_launch": launch, "_delegation_completed": True, "_delegate_from": "root", "_delegation_active_route": {"provider": selector, "model": model}}
    db = SimpleNamespace(resolve_resume_session_id=lambda sid: sid, get_session=lambda sid: {"model_config": json.dumps(state)}, get_compression_lineage=lambda sid: ["root"])
    parent = SimpleNamespace(session_id="root", _session_db=db)
    def resume():
        return _resolve_resume_launch({"resume_session_id": "child"}, definitions, parent)
    result = resume()
    assert result.credentials["provider"] == "custom"
    assert result.credentials["api_key"] == key
    assert result.definition.provider == selector
    for field, value in (("base_url", "https://other.invalid/v1"), ("api_key", "changed-fixture-key")):
        changed = json.loads(json.dumps(config))
        changed["custom_providers"][0][field] = value
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(changed))
        with pytest.raises(ValueError, match="can no longer be authorized exactly"):
            resume()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    for field, value in (("api_mode", "anthropic_messages"), ("request_overrides", {"extra_headers": {"x-test": "changed"}})):
        original = launch[field]
        launch[field] = value
        with pytest.raises(ValueError, match="can no longer be authorized exactly"):
            resume()
        launch[field] = original
    state["_delegation_user_stopped"] = True
    with pytest.raises(ValueError, match="requires resume_authorization"):
        resume()


@pytest.mark.parametrize("stored_provider", ["custom", "custom:codex-proxy"])
def test_primary_metadata_refresh_preserves_custom_selector(stored_provider):
    from tools.delegate_tool import _refresh_resumable_launch_metadata
    selector = "custom:codex-proxy"
    launch = {"provider": stored_provider, "requested_provider": selector,
              "model": "fixture-model", "credential_pool_entry_id": "old-entry",
              "authority_fingerprint": "old-digest", "fallbacks": []}
    child = SimpleNamespace(provider=selector, model="fixture-model",
                            api_key="refreshed-fixture-key", _credential_pool_entry_id="new-entry")
    updated = _refresh_resumable_launch_metadata(child, launch)
    assert updated["credential_pool_entry_id"] == "new-entry"
    assert updated["authority_fingerprint"] == hashlib.sha256(child.api_key.encode()).hexdigest()
    assert updated["requested_provider"] == selector
    assert launch["credential_pool_entry_id"] == "old-entry"
    child.provider = "custom:other-selector"
    assert _refresh_resumable_launch_metadata(child, launch) == launch
