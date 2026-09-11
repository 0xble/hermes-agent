"""Real task-path authority must apply to every mediated knowledge write."""
import json

import pytest

from agent.delegation_context import delegated_child_context
from tools import file_tools, knowledge_boundary
from tools.terminal_tool import record_session_cwd


@pytest.mark.parametrize("root_name", ["memories", "skills"])
@pytest.mark.parametrize("operation", ["write", "replace", "add", "delete", "move"])
def test_task_relative_knowledge_writes_are_denied(tmp_path, monkeypatch, root_name, operation):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    root = home / root_name
    root.mkdir(parents=True, exist_ok=True)
    target = root / "notes.txt"
    target.write_text("original")
    task = f"knowledge-{root_name}-{operation}"
    record_session_cwd(task, str(root))
    with delegated_child_context(read_only_knowledge=True):
        if operation == "write":
            result = file_tools.write_file_tool("notes.txt", "changed", task_id=task)
        elif operation == "replace":
            result = file_tools.patch_tool(path="notes.txt", old_string="original", new_string="changed", task_id=task)
        else:
            headers = {
                "add": "*** Add File: other.txt\n+new\n",
                "delete": "*** Delete File: notes.txt\n",
                "move": "*** Move File: notes.txt -> other.txt\n",
            }
            result = file_tools.patch_tool(mode="patch", patch="*** Begin Patch\n" + headers[operation] + "*** End Patch", task_id=task)
    assert "Parent-owned shared knowledge" in json.loads(result)["error"]
    assert target.read_text() == "original"
    assert not (root / "other.txt").exists()


@pytest.mark.parametrize("resolver", ["tools.memory_tool.get_memory_dir", "hermes_constants.get_skills_dir"])
def test_root_resolution_failure_refuses_known_child_but_not_parent(monkeypatch, resolver):
    def broken():
        raise RuntimeError("root unavailable")

    monkeypatch.setattr(resolver, broken)
    with delegated_child_context(read_only_knowledge=True):
        assert "could not be evaluated" in knowledge_boundary.write_denial_reason(["notes.txt"])
        assert "could not be evaluated" in knowledge_boundary.command_denial_reason("echo hello")
    assert knowledge_boundary.write_denial_reason(["notes.txt"]) is None
    assert knowledge_boundary.command_denial_reason("echo hello") is None


def test_unknown_task_path_fails_closed_before_write(monkeypatch):
    monkeypatch.setattr(file_tools, "_resolve_or_none", lambda *args: None)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda *args: pytest.fail("must not write"))
    with delegated_child_context(read_only_knowledge=True):
        assert "could not be evaluated" in json.loads(file_tools.write_file_tool("notes.txt", "x"))["error"]
        assert "could not be evaluated" in json.loads(file_tools.patch_tool(path="notes.txt", old_string="x", new_string="y"))["error"]


def test_allowed_task_write_uses_same_checked_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record_session_cwd("ordinary-child", str(workspace))
    resolved = file_tools._resolve_path_for_task
    calls = []

    def resolve_once(path, task):
        calls.append((path, task))
        return resolved(path, task)

    monkeypatch.setattr(file_tools, "_resolve_or_none", lambda path, task: str(resolve_once(path, task)))
    with delegated_child_context(read_only_knowledge=True):
        result = json.loads(file_tools.write_file_tool("notes.txt", "allowed", task_id="ordinary-child"))
    assert not result.get("error")
    assert (workspace / "notes.txt").read_text() == "allowed"
    assert calls == [("notes.txt", "ordinary-child")]
