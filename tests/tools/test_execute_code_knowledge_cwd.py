"""Guard the local kernel's current cwd, including persistent chdir and staging."""
import json
import sys

import pytest

from agent.delegation_context import delegated_child_context
from tools import code_kernel
from tools.code_execution_tool import execute_code


@pytest.fixture
def execution(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    protected = home / "memories"
    protected.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr("tools.code_execution_tool._load_config", lambda: {"mode": "strict", "timeout": 10})
    monkeypatch.setattr("tools.code_execution_tool._resolve_child_python", lambda mode: sys.executable)
    code_kernel.shutdown_all_kernels()
    def run(code):
        return json.loads(execute_code(code, task_id="knowledge-cwd"))
    with delegated_child_context(read_only_knowledge=False):
        yield run, protected
    code_kernel.shutdown_all_kernels()


def test_persistent_actual_cwd_denial_preserves_kernel(execution):
    run, protected = execution
    assert run(f"import os; marker = 42; os.chdir({str(protected)!r})")["status"] == "success"
    with delegated_child_context(read_only_knowledge=True):
        denied = run("open('notes.txt', 'w').write('bad')")
    assert "Parent-owned shared knowledge" in denied["error"]
    assert not (protected / "notes.txt").exists()
    # Refusal does not erase interpreter state or ordinary writable-child authority.
    result = run("print(marker); open('notes.txt', 'w').write('authorized')")
    assert result["status"] == "success"
    assert "42" in result["output"]
    assert protected.joinpath("notes.txt").read_text() == "authorized"


def test_strict_staging_relative_traversal_uses_actual_cwd(execution):
    import os
    run, protected = execution
    result = run("import os; print(os.getcwd())")
    cwd = result["output"].strip()
    relative = os.path.relpath(protected / "notes.txt", cwd)
    with delegated_child_context(read_only_knowledge=True):
        denied = run(f"open({relative!r}, 'w').write('bad')")
        allowed = run("print(6 * 7)")
    assert "Parent-owned shared knowledge" in denied["error"]
    assert not (protected / "notes.txt").exists()
    assert allowed["status"] == "success"


def test_cwd_lookup_failure_refuses_only_guarded_cell(execution, monkeypatch):
    import psutil
    run, protected = execution
    assert run("marker = 42")["status"] == "success"
    def unavailable(self):
        raise psutil.AccessDenied(self.pid)
    monkeypatch.setattr(psutil.Process, "cwd", unavailable)
    with delegated_child_context(read_only_knowledge=True):
        denied = run("print(marker)")
    assert "could not be evaluated" in denied["error"]
    assert run("print(marker)")["status"] == "success"


def test_project_task_cwd_is_checked_on_first_cell(execution, monkeypatch):
    from tools.terminal_tool import record_session_cwd
    run, protected = execution
    monkeypatch.setattr("tools.code_execution_tool._load_config", lambda: {"mode": "project", "timeout": 10})
    record_session_cwd("knowledge-cwd", str(protected))
    with delegated_child_context(read_only_knowledge=True):
        result = run("open('notes.txt', 'w').write('bad')")
    assert "Parent-owned shared knowledge" in result["error"]
    assert not (protected / "notes.txt").exists()
