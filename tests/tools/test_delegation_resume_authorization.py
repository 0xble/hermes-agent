"""Owner-authorized continuation does not confuse cancellation with a user stop."""
import json
import threading
from types import SimpleNamespace

import pytest

from agent.interrupt_control import InterruptControlMixin
from hermes_state import SessionDB
from tools.delegate_tool_checkpoint import checkpoint_child_resume


def agent_stub():
    return SimpleNamespace(_execution_thread_id=None, _active_children_lock=threading.Lock(),
                           _active_children=[], quiet_mode=True)


@pytest.mark.parametrize("reason,stopped", [(None, False), ("delegation timeout", False),
    ("delegation cancelled", False), ("explicit stop requested", True)])
def test_soft_interrupt_is_not_implicit_stop(reason, stopped):
    child = agent_stub()
    InterruptControlMixin.interrupt(child, tool_reason=reason)
    assert bool(getattr(child, "_delegation_user_stopped", False)) is stopped


def test_internal_child_stop_retains_cancellation_provenance():
    from tools.delegate_tool_child_run import _signal_child_stop
    class Child(InterruptControlMixin):
        pass
    child = Child()
    child.__dict__.update(vars(agent_stub()))
    _signal_child_stop(child)
    assert not getattr(child, "_delegation_user_stopped", False)
    assert child._tool_interrupt_reason == "delegation cancelled"


def stopped_fixture(tmp_path, monkeypatch):
    from tests.run_agent.test_delegation_frozen_runtime import _resume_fixture
    metadata, definitions, _ = _resume_fixture(monkeypatch)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("root", source="cli")
    db.create_session("child", source="tool", model_config={"_delegation_launch": metadata,
        "_delegate_from": "root", "_delegation_completed": False,
        "_delegation_outcome": "interrupted", "_delegation_user_stopped": True,
        "_delegation_resume_blocked_reason": "user_stopped_requires_explicit_authorization"})
    db.append_message("child", role="user", content="Original task")
    return db, definitions, SimpleNamespace(session_id="root", _session_db=db)


def authorization():
    return {"authorization": "User explicitly asked to resume this stopped child.",
            "reconciliation": "Read durable transcript and worktree; no unresolved external effects or processes."}


def test_stopped_child_explicit_resume_is_atomic_and_single_use(tmp_path, monkeypatch):
    from tools.delegate_tool import _resolve_resume_launch
    db, definitions, parent = stopped_fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="resume_authorization"):
        _resolve_resume_launch({"resume_session_id": "child"}, definitions, parent)
    launch = _resolve_resume_launch({"resume_session_id": "child", "resume_authorization": authorization()}, definitions, parent)
    assert launch.resume_session_id == "child"
    assert launch.credentials["model"] == "m"
    token = {"child": launch.resume_recovery}
    assert db.claim_delegated_resumes(["child"], claim_id="first", reconciliations=token)
    assert not db.claim_delegated_resumes(["child"], claim_id="duplicate", reconciliations=token)
    assert db.release_delegated_resumes(["child"], claim_id="first")
    config = json.loads(db.get_session("child")["model_config"])
    assert config["_delegation_user_stopped"] is True
    assert config["_delegation_completed"] is False
    assert config["_delegation_resume_authorizations"][0]["claim_id"] == "first"
    db.close()


@pytest.mark.parametrize("change", ["new_stop", "message", "lease", "foreign", "unknown_tool"])
def test_resume_authorization_never_overrides_new_state_or_unknown_effects(tmp_path, monkeypatch, change):
    from tools.delegate_tool import _resolve_resume_launch
    db, definitions, parent = stopped_fixture(tmp_path, monkeypatch)
    task = {"resume_session_id": "child", "resume_authorization": authorization()}
    if change == "foreign":
        parent.session_id = "other"
        with pytest.raises(ValueError, match="foreign"):
            _resolve_resume_launch(task, definitions, parent)
        db.close()
        return
    launch = _resolve_resume_launch(task, definitions, parent)
    if change == "new_stop":
        db.patch_session_model_config("child", {"_delegation_interrupt_reason": "explicit stop requested"})
    elif change == "message":
        db.append_message("child", role="assistant", content="Newer checkpoint")
    elif change == "lease":
        assert db.acquire_session_turn_lease("child", "active", ttl_seconds=60)
    else:
        db.append_message("child", role="assistant", tool_calls=[{"id":"write", "type":"function", "function":{"name":"terminal", "arguments":"{}"}}])
    assert not db.claim_delegated_resumes(["child"], claim_id="stale", reconciliations={"child": launch.resume_recovery})
    assert json.loads(db.get_session("child")["model_config"])["_delegation_user_stopped"] is True
    db.close()


def test_new_genuine_stop_requires_new_authorization(tmp_path, monkeypatch):
    from tools.delegate_tool import _resolve_resume_launch
    db, definitions, parent = stopped_fixture(tmp_path, monkeypatch)
    launch = _resolve_resume_launch({"resume_session_id": "child", "resume_authorization": authorization()}, definitions, parent)
    assert db.claim_delegated_resumes(["child"], claim_id="first", reconciliations={"child": launch.resume_recovery})
    child = SimpleNamespace(_session_db=db, session_id="child", _delegation_named_type="advisor",
                            _delegation_user_stopped=True, provider="fixture", model="m")
    checkpoint_child_resume(child, {"messages":db.get_messages_as_conversation("child")}, {"status":"interrupted"})
    with pytest.raises(ValueError, match="resume_authorization"):
        _resolve_resume_launch({"resume_session_id":"child"}, definitions, parent)
    assert not db.claim_delegated_resumes(["child"], claim_id="replay", reconciliations={"child": launch.resume_recovery})
    db.close()


@pytest.mark.parametrize("status", ["timeout", "error"])
def test_failure_without_result_never_grants_resume_while_worker_can_write(tmp_path, monkeypatch, status):
    from tools.delegate_tool import _resolve_resume_launch
    db, definitions, parent = stopped_fixture(tmp_path, monkeypatch)
    db.patch_session_model_config("child", {"_delegation_user_stopped": False})
    assert db.acquire_session_turn_lease("child", "old-worker", ttl_seconds=60)
    child = SimpleNamespace(_session_db=db, session_id="child", _delegation_named_type="advisor")
    entry = {"status": status}
    checkpoint_child_resume(child, None, entry, child_task_id="old-worker")
    assert not entry["resume_available"]
    assert json.loads(db.get_session("child")["model_config"])["_delegation_completed"] is False
    assert not db.claim_delegated_resumes(["child"], claim_id="no-receipt")
    token = _resolve_resume_launch({"resume_session_id": "child", "resume_authorization": authorization()},
                                   definitions, parent).resume_recovery
    assert not db.claim_delegated_resumes(["child"], claim_id="live-worker", reconciliations={"child": token})
    db.append_message("child", role="assistant", content="Late worker checkpoint")
    db.release_session_turn_lease("child", "old-worker")
    assert not db.claim_delegated_resumes(["child"], claim_id="stale-receipt", reconciliations={"child": token})
    db.close()


@pytest.mark.parametrize("producer", ["ctrl_c", "ctrl_q", "subagent"])
def test_actual_user_stop_producers_retain_stop_provenance(monkeypatch, producer):
    class Child(InterruptControlMixin):
        pass
    child = Child()
    child.__dict__.update(vars(agent_stub()))
    if producer == "subagent":
        from tools import delegate_tool_registry as registry
        monkeypatch.setitem(registry._active_subagents, "sa-stop-fixture", {"agent": child})
        assert registry.interrupt_subagent("sa-stop-fixture")
    else:
        from hermes_cli.cli_tui_mixin import CLITuiMixin
        cli = SimpleNamespace(agent=child, _agent_running=True, _last_ctrl_c_time=0,
            _tui_cancel_voice_recording=lambda event: False,
            _tui_cancel_foreground_ui=lambda *args, **kwargs: False,
            _tui_clear_blocking_overlays=lambda event: False,
            _close_model_picker=lambda: None, _close_command_palette=lambda: None)
        getattr(CLITuiMixin, "_tui_handle_" + producer)(cli, None)
    assert child._delegation_user_stopped
    assert child._delegation_stop_token
