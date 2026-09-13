"""Interrupted checkpoints freeze identity without granting unsafe continuation."""
import hashlib
import json
from types import SimpleNamespace

import pytest
import yaml

from hermes_state import SessionDB
from tools.custom_subagents import freeze_fallback_routes, parse_definitions, resolve_named_credentials
from tools.delegate_tool import _resolve_resume_launch
from tools.delegate_tool_checkpoint import checkpoint_child_resume


@pytest.mark.parametrize("transition", ["fallback", "rotated_account"])
def test_blocked_checkpoint_retains_route_until_effect_reconciliation(tmp_path, monkeypatch, transition):
    from tools import process_registry as pr

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    monkeypatch.setattr("hermes_cli.runtime_provider._try_resolve_from_custom_pool", lambda *a, **kw: None)
    config = {"providers": {name: {"base_url": f"https://{name}.invalid/v1",
        "api_key": f"synthetic-{name}", "default_model": f"{name}-model"} for name in ("primary", "fallback")}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    cfg = {"subagents": {"advisor": {"description": "Advise", "instructions": "Analyze",
        "provider": "custom:primary", "model": "primary-model",
        "fallbacks": [{"provider": "custom:fallback", "model": "fallback-model"}]}}}
    definitions = parse_definitions(cfg)
    old = SimpleNamespace(id="old-account", provider="custom:primary", runtime_api_key="synthetic-primary",
                          runtime_base_url="https://primary.invalid/v1")
    rotated = SimpleNamespace(id="rotated-account", provider="custom:primary", runtime_api_key="synthetic-rotated",
                              runtime_base_url="https://primary.invalid/v1")
    pool = SimpleNamespace(provider="custom:primary", entries=lambda: [old, rotated])
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="cli")
    parent = SimpleNamespace(session_id="root", _session_db=db, provider="custom",
                             base_url=old.runtime_base_url, _credential_pool=pool)
    creds, _ = resolve_named_credentials(definitions["advisor"], cfg, parent)
    routes = freeze_fallback_routes(definitions["advisor"], primary_provider="custom", primary_model="primary-model")
    launch = {"version": 1, "subagent_type": "advisor", "parent_session_root": "root",
        "provider": "custom", "requested_provider": "custom:primary", "model": "primary-model",
        "base_url": creds["base_url"], "api_mode": creds["api_mode"],
        "authority_fingerprint": hashlib.sha256(old.runtime_api_key.encode()).hexdigest(),
        "credential_pool_entry_id": old.id, "request_overrides": {}, "fallback_source": "role",
        "fallbacks": [route.metadata() for route in routes]}
    db.create_session("child", source="tool", model_config={"_delegate_from": "root", "_delegation_launch": launch,
        "_delegation_resume_claimed_at": "old-claim", "_delegation_completed": False})
    messages = [{"role": "user", "content": "Continue the work"}, {"role": "assistant", "content": "Stopped at checkpoint"}]
    for row in messages:
        db.append_message("child", **row)
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    process = pr.ProcessSession(id="proc_unreconciled", command="fixture effect", owner_task_id="child-run", parent_session_id="child")
    registry._running[process.id] = process
    fallback = transition == "fallback"
    child = SimpleNamespace(_session_db=db, session_id="child", _delegation_named_type="advisor",
        _delegation_launch_metadata=launch, provider="custom", model="fallback-model" if fallback else "primary-model",
        api_key="synthetic-fallback" if fallback else rotated.runtime_api_key,
        _credential_pool_entry_id=None if fallback else rotated.id, _process_owner_task_ids={"child-run"})
    entry = {"status": "interrupted"}
    task = {"resume_session_id": "child", "resume_authorization": {
        "authorization": "Parent authorizes this continuation", "reconciliation": "Process result observed"}}
    try:
        checkpoint_child_resume(child, {"messages": messages}, entry, child_task_id="child-run")
        assert entry["resume_available"] is False
        db.close()
        db = parent._session_db = SessionDB(tmp_path / "state.db")
        state = json.loads(db.get_session("child")["model_config"])
        assert state["_delegation_completed"] is False
        assert state.get("_delegation_resume_claimed_at") is None
        assert state["_delegation_resume_blocked_reason"] == "unreconciled_background_processes"
        if fallback:
            assert state.get("_delegation_active_route") == {"provider": "custom", "model": "fallback-model"}
        else:
            assert state["_delegation_launch"]["credential_pool_entry_id"] == rotated.id
        assert "synthetic-" not in db.get_session("child")["model_config"]
        with pytest.raises(ValueError, match="Reconcile background process"):
            _resolve_resume_launch(task, definitions, parent, defaults=cfg)
        process.exited, process.exit_code = True, 0
        process.output_buffer = "effect completed"
        registry._move_to_finished(process)
        registry.read_log(process.id)
        resumed = _resolve_resume_launch(task, definitions, parent, defaults=cfg)
        assert resumed.credentials["model"] == child.model
        assert resumed.credentials["api_key"] == child.api_key
        assert resumed.resume_recovery is not None
        assert launch["credential_pool_entry_id"] == old.id
    finally:
        db.close()
