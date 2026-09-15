"""Static pathlib operands follow actual constructor semantics before admission."""
import json
import shlex
from pathlib import Path

import pytest

from agent.delegation_context import delegated_child_context
from tools.knowledge_boundary import command_denial_reason, execution_denial_reason
from tools.knowledge_command_paths import literal_paths


@pytest.mark.parametrize("tool", ["execute_code", "terminal"])
@pytest.mark.parametrize("form", ["components", "reset", "nested", "relative"])
def test_multicomponent_ancestor_is_denied_before_execution(tmp_path, monkeypatch, tool, form):
    home = tmp_path / "parent" / "home"
    protected = home / "memories"
    protected.mkdir(parents=True)
    (protected / "keep").write_text("original")
    monkeypatch.setenv("HERMES_HOME", str(home))
    expressions = {
        "components": f"P({str(tmp_path)!r}, 'parent', 'home')",
        "reset": f"P('discarded', {str(tmp_path)!r}, 'parent', 'home')",
        "nested": f"P(P({str(tmp_path)!r}, 'parent'), 'home')",
        "relative": "P('parent', 'home')",
    }
    code = f"from pathlib import Path as P; import shutil; shutil.rmtree({expressions[form]})"
    command = code if tool == "execute_code" else "python -c " + shlex.quote(code)
    with delegated_child_context(read_only_knowledge=True):
        denial = command_denial_reason(command, tool=tool, cwd=str(tmp_path))
    assert denial and "Parent-owned shared knowledge" in denial
    if tool == "execute_code" and form == "relative":
        # Relative paths are checked at the kernel's actual execution cwd boundary.
        with delegated_child_context(read_only_knowledge=True):
            assert execution_denial_reason(code, current_cwd=lambda: str(tmp_path))
    elif tool == "execute_code":
        from tools import code_execution_tool as execute
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("tools.code_kernel.execute_in_session_kernel", lambda *a, **k: pytest.fail("spawned code"))
        with delegated_child_context(read_only_knowledge=True):
            result = json.loads(execute.execute_code(code, task_id="path-components"))
        assert "Parent-owned shared knowledge" in result["error"]
    else:
        from tools import terminal_tool as terminal
        monkeypatch.setattr(terminal, "_acquire_env", lambda *a: pytest.fail("spawned terminal"))
        with delegated_child_context(read_only_knowledge=True):
            result = json.loads(terminal.terminal_tool(command, workdir=str(tmp_path), task_id="path-components"))
        assert "Parent-owned shared knowledge" in result["error"]
    assert (protected / "keep").read_text() == "original"


@pytest.mark.parametrize("parts", [("a", "b"), ("", "a", ""), ("discarded", "/tmp", "b"), ("a", ".", "b")])
def test_literal_components_match_native_pathlib(parts):
    code = f"import pathlib; import shutil; shutil.rmtree(pathlib.Path({', '.join(repr(p) for p in parts)}))"
    assert literal_paths(code, python_source=True)[1] == [str(Path(*parts))]


@pytest.mark.parametrize("expression", ["Path(root, 'home')", "Path('parent', computed())", "Path(*parts)"])
def test_dynamic_components_remain_unresolved(expression):
    code = f"from pathlib import Path; import shutil; shutil.rmtree({expression})"
    assert literal_paths(code, python_source=True)[1] == []


def test_absolute_component_reset_does_not_target_discarded_ancestor(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    other = tmp_path / "other"
    code = f"from pathlib import Path; import shutil; shutil.rmtree(Path({str(home)!r}, {str(other)!r}))"
    assert literal_paths(code, python_source=True)[1] == [str(other)]
    with delegated_child_context(read_only_knowledge=True):
        assert command_denial_reason(code, tool="execute_code", cwd=str(tmp_path)) is None
