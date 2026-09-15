"""An unreadable receipt directory must not look like an empty owner barrier."""

import os
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tools.delegate_tool_checkpoint import checkpoint_child_resume
from tools import process_registry as pr
from tools import process_registry_results as receipts


def test_unreadable_receipt_directory_blocks_reopened_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    session = pr.ProcessSession(id="proc_unreadable", command="effect", owner_task_id="child-run",
                               parent_session_id="child", exited=True, output_buffer="effect outcome")
    assert receipts.save_completed_result(session)
    directory = tmp_path / "logs" / "process-results"
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("child", source="tool", model_config={"_delegation_completed": False})
        messages = [{"role": "user", "content": "Work"}]
        db.append_message("child", **messages[0])
        child = SimpleNamespace(_session_db=db, session_id="child", _delegation_named_type="advisor",
                                provider="fixture", model="m", _process_owner_task_ids={"child-run"})
        # Fault at the OS enumeration boundary, not the result loader. This also
        # exercises PermissionError under root, where chmod cannot deny access.
        def deny_directory(real):
            def enumerate_path(path=".", *args, **kwargs):
                if path == directory or path == str(directory):
                    raise PermissionError("receipt directory unreadable")
                return real(path, *args, **kwargs)
            return enumerate_path

        with monkeypatch.context() as denied:
            denied.setattr(os, "scandir", deny_directory(os.scandir))
            denied.setattr(os, "listdir", deny_directory(os.listdir))
            with pytest.raises(PermissionError):
                registry.unresolved_owned_processes({"child-run"})
            entry = {"status": "interrupted"}
            checkpoint_child_resume(child, {"messages": messages}, entry, child_task_id="child-run")
            assert entry["resume_available"] is False
            assert not db.claim_delegated_resumes(["child"], claim_id="unreadable")
        assert [s.id for s in registry.unresolved_owned_processes({"child-run"})] == [session.id]


def test_absent_receipt_directory_is_empty_first_use(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert not (tmp_path / "logs" / "process-results").exists()
    assert pr.ProcessRegistry().unresolved_owned_processes({"child-run"}) == []
