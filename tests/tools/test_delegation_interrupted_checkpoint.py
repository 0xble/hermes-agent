"""Conservative interrupted continuation using actual session DB and lease code."""
import json
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tools.delegate_tool_checkpoint import checkpoint_child_resume


@pytest.mark.parametrize("boundary,allowed", [
    ("model_response", True), ("tool_running", False), ("external_write_unreceipted", False),
    ("cancelled_tool", False), ("result_before_persist", False), ("tool_checkpoint", True),
    ("user_stop", False), ("missing_checkpoint", False),
])
def test_interrupted_checkpoint_boundaries(tmp_path, boundary, allowed):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", source="tool", model_config={"_delegation_completed": False})
    messages = [{"role": "user", "content": "Authorized task"}]
    if boundary not in {"missing_checkpoint"}:
        db.append_message("child", role="user", content="Authorized task")
    if boundary in {"tool_running", "external_write_unreceipted", "cancelled_tool", "result_before_persist", "tool_checkpoint"}:
        calls = [{"id": "write", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]
        messages.append({"role": "assistant", "content": "", "tool_calls": calls})
        db.append_message("child", role="assistant", content="", tool_calls=calls)
        if boundary in {"cancelled_tool", "result_before_persist", "tool_checkpoint"}:
            content = '{"error_type":"tool_interrupted"}' if boundary == "cancelled_tool" else "verified result"
            messages.append({"role": "tool", "content": content, "tool_call_id": "write"})
            if boundary != "result_before_persist":
                db.append_message("child", role="tool", content=content, tool_call_id="write")
    child = SimpleNamespace(_session_db=db, session_id="child", _delegation_named_type="advisor",
                            _delegation_user_stopped=boundary == "user_stop", provider="fixture", model="m")
    entry = {"status": "interrupted"}
    checkpoint_child_resume(child, {"messages": messages}, entry)
    assert entry["resume_available"] is allowed
    config = json.loads(db.get_session("child")["model_config"])
    assert config["_delegation_completed"] is allowed
    if allowed:
        assert config["_delegation_outcome"] == "interrupted"
        assert entry["resume_session_id"] == "child"
        db.close()
        db = SessionDB(db_path=tmp_path / "state.db")
        assert db.claim_delegated_resumes(["child"], claim_id="first")
        assert not db.claim_delegated_resumes(["child"], claim_id="duplicate")
        assert not db.release_delegated_resumes(["child"], claim_id="other-owner-claim")
        assert db.release_delegated_resumes(["child"], claim_id="first")
    else:
        assert entry.get("resume_blocked_reason")
    db.close()


def test_interrupted_resume_freezes_route_owner_and_context(tmp_path, monkeypatch):
    from tests.run_agent.test_delegation_frozen_runtime import _resume_fixture
    from tools.delegate_tool import _resolve_resume_launch
    metadata, definitions, _ = _resume_fixture(monkeypatch)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("root", source="cli")
    db.create_session("child", source="tool", cwd=str(tmp_path), model_config={"_delegation_launch": metadata,
        "_delegation_completed": True, "_delegation_outcome": "interrupted", "_delegate_from": "root"})
    db.append_message("child", role="user", content="Original context")
    parent = SimpleNamespace(session_id="root", _session_db=db)
    launch = _resolve_resume_launch({"resume_session_id": "child"}, definitions, parent)
    assert launch.resume_session_id == "child"
    assert launch.workspace_path == str(tmp_path)
    assert launch.credentials["provider"] == "fixture" and launch.credentials["model"] == "m"
    assert launch.credentials["base_url"] == "https://fixture/v1"
    assert db.get_messages_as_conversation("child")[0]["content"] == "Original context"
    db.create_session("other", source="cli")
    with pytest.raises(ValueError, match="foreign"):
        _resolve_resume_launch({"resume_session_id": "child"}, definitions, SimpleNamespace(session_id="other", _session_db=db))
    db.patch_session_model_config("child", {"_delegation_user_stopped": True})
    with pytest.raises(ValueError, match="User-stopped"):
        _resolve_resume_launch({"resume_session_id": "child"}, definitions, parent)
    db.close()


def test_active_lease_blocks_duplicate_interrupted_resume(tmp_path):
    db = SessionDB(db_path=tmp_path / "active.db")
    db.create_session("child", source="tool", model_config={"_delegation_completed": True})
    assert db.acquire_session_turn_lease("child", "running-owner", ttl_seconds=60)
    assert not db.claim_delegated_resumes(["child"], claim_id="would-duplicate")
    db.release_session_turn_lease("child", "running-owner")
    assert db.claim_delegated_resumes(["child"], claim_id="safe")
    db.close()


@pytest.mark.parametrize("content", ["[Command interrupted]", "[Orphan recovery: effect is UNKNOWN]",
                                    '{"error_type":"tool_interrupted"}'])
def test_real_cancellation_and_orphan_formats_stay_unresolved(content):
    from tools.delegate_tool_checkpoint import unresolved_tool_result
    assert unresolved_tool_result(content)
    assert not unresolved_tool_result("Read a document discussing tool_interrupted handling")
