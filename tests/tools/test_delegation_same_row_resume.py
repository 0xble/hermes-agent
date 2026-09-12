"""Normal resume dispatch restores a real interrupted checkpoint and card identity."""
import json
from copy import deepcopy
from types import SimpleNamespace
import pytest

from hermes_state import SessionDB
from tools import delegate_tool


@pytest.mark.parametrize("field,value,diagnostic", [
    ("provider", "PRIVATE-PROVIDER", "provider"),
    ("model", "PRIVATE-MODEL", "model"),
    ("api_mode", "PRIVATE-MODE", "api_mode"),
    ("base_url", "https://private.invalid/v1", "base_url"),
    ("api_key", "PRIVATE-CREDENTIAL", "authority"),
    ("request_overrides", {"authorization": "PRIVATE-OVERRIDE"}, "request_overrides"),
])
def test_resume_reports_only_mismatched_field_names(monkeypatch, field, value, diagnostic):
    from tests.run_agent.test_delegation_frozen_runtime import _resume_fixture
    metadata, definitions, parent = _resume_fixture(monkeypatch)
    creds = {"provider": "fixture", "model": "m", "base_url": "https://fixture/v1",
             "api_key": "secret", "api_mode": "chat_completions"}
    creds[field] = value
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: creds)
    with pytest.raises(ValueError) as error:
        delegate_tool._resolve_resume_launch({"resume_session_id": "child"}, definitions, parent)
    assert str(error.value) == ("delegated child primary route can no longer be authorized exactly; "
                                "mismatched fields: " + diagnostic)
    assert metadata["authority_fingerprint"] not in str(error.value)


def test_resume_accepts_equivalent_normalized_route_without_repinning(monkeypatch):
    from tests.run_agent.test_delegation_frozen_runtime import _resume_fixture
    metadata, definitions, parent = _resume_fixture(monkeypatch)
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: {
        "provider": "fixture", "model": "m", "base_url": "https://fixture/v1/",
        "api_key": "secret", "api_mode": "chat_completions"})
    launch = delegate_tool._resolve_resume_launch({"resume_session_id": "child"}, definitions, parent)
    assert launch.launch_metadata == metadata
    assert launch.reasoning == {"enabled": True, "effort": "high"}


@pytest.mark.parametrize("corrupt", [False, True])
def test_normal_resume_dispatch_restores_logical_identity(tmp_path, monkeypatch, corrupt):
    from tests.run_agent.test_delegation_frozen_runtime import _resume_fixture
    metadata, _, _ = _resume_fixture(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    owner = {"profile": str(tmp_path), "session_id": "root", "session_key": "", "chat_id": "", "thread_id": "", "topic_id": ""}
    from tools.async_delegation import reserve_delegation_metadata
    original = reserve_delegation_metadata(parent_task_id=None, owner=owner, task_labels=["Refine layering skill"])
    identity = {"parent_task_id": original["parent_task_id"], "thread_ref": "A", "task_label": "Refine layering skill", "owner": owner, "attempt": 0}
    if corrupt:
        identity.pop("thread_ref")
    metadata["card_identity"] = identity
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("root", source="cli")
    db.create_session("child", source="tool", cwd=str(tmp_path), model_config={"_delegation_launch": metadata, "_delegate_from": "root", "_delegation_completed": False, "_delegation_outcome": "interrupted"})
    db.append_message("child", role="user", content="Original authorized context")
    from tools.delegate_tool_checkpoint import checkpoint_child_resume
    interrupted = SimpleNamespace(session_id="child", _session_db=db, _delegation_named_type="advisor", _delegation_launch_metadata=metadata, provider="fixture", model="m")
    entry = {"status": "interrupted"}
    checkpoint_child_resume(interrupted, {"messages": db.get_messages_as_conversation("child")}, entry)
    assert entry["resume_available"]
    db.close()
    db = SessionDB(db_path=tmp_path / "state.db")
    parent = SimpleNamespace(session_id="root", _session_db=db, _delegate_depth=0,
        _delegation_visible_window=[{"role": "user", "content": "NEW_PARENT_CONTEXT_NOT_FOR_RESUME"}])
    cfg = {"subagents": {"advisor": {"description": "Advise", "instructions": "Analyze", "provider": "fixture", "model": "m", "reasoning_effort": "high"}}}
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    monkeypatch.setattr(delegate_tool, "last_delegation_config_error", lambda: None)
    monkeypatch.setattr(delegate_tool, "_resolve_delegation_credentials", lambda *_: {})
    monkeypatch.setattr("tools.delegation_live_log.create_live_transcripts", lambda *a, **kw: (None, [], []))
    monkeypatch.setattr(delegate_tool, "_announce_batch", lambda *a: None)
    monkeypatch.setattr(delegate_tool, "_capture_origin", lambda: (None, None, None, None, None))
    built = []

    def build(tasks, *args, task_runtime, **kwargs):
        launch = task_runtime[0]
        assert launch.resume_session_id == "child"
        assert launch.workspace_path == str(tmp_path)
        assert launch.credentials["model"] == "m"
        assert launch.credentials["provider"] == "fixture"
        child = SimpleNamespace(session_id="child", _delegate_role="advisor", _delegation_named_type="advisor", _progress_identity_ref={"session_id": "child"}, _delegation_launch_metadata=deepcopy(launch.launch_metadata))
        built.append(child)
        return [(0, tasks[0], child)], None

    monkeypatch.setattr(delegate_tool, "_build_children", build)
    monkeypatch.setattr(delegate_tool, "_run_batch", lambda batch, background: json.dumps(batch.delegation_metadata))
    payload = json.loads(delegate_tool.delegate_task(tasks=[{"goal": "Continue safely", "task_label": "Recover renamed", "resume_session_id": "child"}], parent_agent=parent, background=False))
    if corrupt:
        assert "Legacy/uncheckpointed identity" in payload["error"]
        assert not built
        assert db.claim_delegated_resumes(["child"], claim_id="restored-after-refusal")
        db.close()
        return
    assert "error" not in payload, payload
    assert payload["parent_task_id"] == identity["parent_task_id"]
    assert payload["thread_refs"] == ["A"]
    assert payload["task_labels"] == ["Refine layering skill"]
    assert payload["attempts"] == {"A": 1}
    assert built[0]._progress_identity_ref["session_id"] == "child"
    assert built[0]._delegation_context_mode == "resume"
    assert not hasattr(built[0], "_delegation_fork_history")
    assert payload["threads"][0]["context_mode"] == "resume"
    assert db.get_messages_as_conversation("child")[0]["content"] == "Original authorized context"
    assert not db.claim_delegated_resumes(["child"], claim_id="duplicate")
    following = reserve_delegation_metadata(parent_task_id=None, owner=owner, task_labels=["New work"])
    assert following["thread_refs"] == ["B"]  # continuation allocated no new logical ref
    db.close()
