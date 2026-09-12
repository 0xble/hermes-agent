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


@pytest.mark.parametrize("root_name", ["memories", "skills"])
@pytest.mark.parametrize("source", ["explicit", "relative", "symlink", "recorded", "default"])
@pytest.mark.parametrize("background", [False, True])
def test_terminal_protected_execution_directory_is_denied_before_acquisition(
    tmp_path, monkeypatch, root_name, source, background,
):
    from types import SimpleNamespace
    from tools import terminal_tool as terminal
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = home / root_name
    root.mkdir(parents=True)
    target = root / "MEMORY.md"
    target.write_text("original")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    session = f"terminal-{root_name}-{source}-{background}"
    terminal.record_session_cwd(session, str(root if source == "recorded" else tmp_path))
    monkeypatch.setattr("tools.approval.get_current_session_key", lambda **k: session if source != "default" else "")
    if source == "default":
        session = None
    monkeypatch.setattr(terminal, "_plan_execution", lambda *a, **k: SimpleNamespace(
        cwd=str(root if source == "default" else tmp_path), env_type="local", effective_task_id="test"))
    monkeypatch.setattr(terminal, "_acquire_env", lambda *a: pytest.fail("acquired environment before denial"))
    workdir = str(alias if source == "symlink" else root) if source in {"explicit", "symlink"} else None
    if source == "relative":
        workdir = f"hermes/{root_name}"
    with delegated_child_context(read_only_knowledge=True):
        result = terminal.terminal_tool("printf changed > MEMORY.md", workdir=workdir,
                                        task_id=session, background=background)
    assert "Parent-owned shared knowledge" in json.loads(result)["error"]
    assert target.read_text() == "original"


@pytest.mark.parametrize("background", [False, True])
def test_terminal_binds_checked_cwd_even_if_session_record_changes(tmp_path, monkeypatch, background):
    from types import SimpleNamespace
    from tools import terminal_tool as terminal
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = home / "memories"
    root.mkdir(parents=True)
    session = "cwd-bind-test"
    terminal.record_session_cwd(session, str(tmp_path))
    monkeypatch.setattr("tools.approval.get_current_session_key", lambda **k: session)
    monkeypatch.setattr(terminal, "_plan_execution", lambda *a, **k: SimpleNamespace(
        cwd=str(tmp_path), env_type="local", effective_task_id=session, effective_timeout=1,
        config={}, promoted_from_foreground_timeout=None))
    def acquire(*args):
        terminal.record_session_cwd(session, str(root))
        return SimpleNamespace()
    monkeypatch.setattr(terminal, "_acquire_env", acquire)
    monkeypatch.setattr(terminal, "_pre_exec_block", lambda *a, **k: None)
    monkeypatch.setattr(terminal, "_run_approval_guards", lambda *a, **k: SimpleNamespace(note=None, approved_run=False))
    captured = []
    def run(*a, **kwargs):
        captured.append(kwargs)
        return json.dumps({"exit_code": 0})
    monkeypatch.setattr(terminal, "_run_foreground", run)
    monkeypatch.setattr(terminal, "spawn_background_process", run)
    with delegated_child_context(read_only_knowledge=True):
        assert json.loads(terminal.terminal_tool("echo ok", task_id=session, background=background))["exit_code"] == 0
    assert captured[0]["execution_cwd"] == str(tmp_path.resolve())
    assert captured[0]["workdir"] is None  # preserve foreground cd bookkeeping


@pytest.mark.parametrize("command", ['python -c "open(\'memories/MEMORY.md\',\'w\').write(\'x\')"', "printf changed > memories/MEMORY.md", "printf changed > './memories/MEMORY.md'", "cd memories && printf changed > MEMORY.md", "printf changed > ../hermes/memories/MEMORY.md"])
@pytest.mark.parametrize("background", [False, True])
def test_literal_relative_shell_reference_is_denied_before_environment(tmp_path, monkeypatch, command, background):
    from tools import terminal_tool as terminal
    home = tmp_path / "hermes"
    (home / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(terminal, "_acquire_env", lambda *a: pytest.fail("acquired environment"))
    with delegated_child_context(read_only_knowledge=True):
        result = json.loads(terminal.terminal_tool(command, workdir=str(home), task_id="relative-guard", background=background))
    assert "Parent-owned shared knowledge" in result["error"]


def test_relative_scanner_preserves_unrelated_shell_and_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    with delegated_child_context(read_only_knowledge=True):
        assert knowledge_boundary.command_denial_reason("printf changed > ordinary.txt", cwd=str(tmp_path)) is None
    assert knowledge_boundary.command_denial_reason("printf changed > memories/MEMORY.md", cwd=str(tmp_path / "hermes")) is None



def test_literal_cd_then_quoted_interpreter_reference_is_denied(tmp_path, monkeypatch):
    from tools import terminal_tool as terminal
    home = tmp_path / "hermes"
    (home / "memories").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(terminal, "_acquire_env", lambda *a: pytest.fail("acquired environment"))
    command = "cd '../hermes'; python -c \"open('memories/MEMORY.md','w').write('x')\""
    with delegated_child_context(read_only_knowledge=True):
        result = json.loads(terminal.terminal_tool(command, workdir=str(workspace), task_id="cd-interpreter"))
    assert "Parent-owned shared knowledge" in result["error"]
