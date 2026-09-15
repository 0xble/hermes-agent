"""A valid live PID does not establish missing checkpoint ownership."""

import json
import os

import pytest

from tools import process_registry as pr


@pytest.mark.parametrize("ownership", [
    {}, {"owner_task_id": None}, {"task_id": None},
    {"owner_task_id": [], "task_id": "shared-container"},
    {"owner_task_id": 17, "task_id": "shared-container"},
    {"owner_task_id": ""}, {"task_id": ""},
])
def test_live_checkpoint_unknown_owner_preserves_resume_fence(tmp_path, monkeypatch, ownership):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    producer = pr.ProcessRegistry()
    entry = {"session_id": "proc_uncertain", "command": "test process", "pid": os.getpid(),
             "pid_scope": "host", "host_start_time": producer._safe_host_start_time(os.getpid()),
             **ownership}
    pr.CHECKPOINT_PATH.write_text(json.dumps([entry]))
    for _ in range(2):
        registry = pr.ProcessRegistry()
        assert registry.recover_from_checkpoint() == 0
        assert registry._running == {}
        assert registry.pending_watchers == []
        assert registry.completion_queue.empty()
        for owner in ("child-run", "shared-container", "unrelated"):
            with pytest.raises(ValueError, match="unresolved owner"):
                registry.unresolved_owned_processes({owner})
        registry._write_checkpoint()
        assert json.loads(pr.CHECKPOINT_PATH.read_text()) == [entry]


@pytest.mark.parametrize("ownership,expected", [
    ({"owner_task_id": "", "task_id": ""}, ""),
    ({"task_id": "legacy-owner"}, "legacy-owner"),
    ({"owner_task_id": "child-run", "task_id": "shared-container"}, "child-run"),
])
def test_live_checkpoint_preserves_explicit_and_legacy_ownership(tmp_path, monkeypatch, ownership, expected):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    producer = pr.ProcessRegistry()
    entry = {"session_id": "proc_known", "command": "test process", "pid": os.getpid(),
             "pid_scope": "host", "host_start_time": producer._safe_host_start_time(os.getpid()),
             **ownership}
    pr.CHECKPOINT_PATH.write_text(json.dumps([entry]))
    registry = pr.ProcessRegistry()
    assert registry.recover_from_checkpoint() == 1
    session = registry._running[entry["session_id"]]
    assert session.owner_task_id == expected and session.detached
    assert registry.unresolved_owned_processes({expected}) == [session]
    assert registry.unresolved_owned_processes({"unrelated"}) == []
