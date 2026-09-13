"""Literal shell operand analysis only, never execute destructive command text."""
import shlex

import pytest

from agent.delegation_context import delegated_child_context
from tools.knowledge_boundary import command_denial_reason
from tools.knowledge_command_paths import literal_paths


@pytest.mark.parametrize("spelling", ["';'", '"&"', r"\|", "'()'", "';'\"|\"", "'\n'"])
@pytest.mark.parametrize("nested", [False, True])
def test_quoted_operators_remain_destructive_operands(tmp_path, monkeypatch, spelling, nested):
    home = tmp_path / "parent" / "home"
    (home / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    target = str(home.parent)
    command = f"rm -rf {spelling} {shlex.quote(target)}"
    if nested:
        command = "sh -c " + shlex.quote(command)
    assert target in literal_paths(command)[1]
    with delegated_child_context(read_only_knowledge=True):
        assert command_denial_reason(command, cwd=str(tmp_path / "elsewhere"))
    # A true control operator ends the rm command, so the following echo is inspection.
    with delegated_child_context(read_only_knowledge=True):
        assert command_denial_reason(f"rm -f harmless; echo {shlex.quote(target)}", cwd=str(tmp_path)) is None


@pytest.mark.macos_only
@pytest.mark.parametrize("shell", [True, False])
@pytest.mark.parametrize("sequence", [list, tuple])
@pytest.mark.parametrize("tool", ["terminal", "execute_code"])
def test_python_subprocess_sequence_uses_shell_flag(tmp_path, monkeypatch, shell, sequence, tool):
    home = tmp_path / "parent" / "home"
    (home / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    target = str(home.parent)
    argv = sequence(["rm -rf " + shlex.quote(target), "ignored-positional"])
    code = f"import subprocess as s; s.run(args={argv!r}, shell={shell})"
    command = code if tool == "execute_code" else "python -c " + shlex.quote(code)
    paths = literal_paths(command, python_source=tool == "execute_code")[1]
    assert (target in paths) is shell
    with delegated_child_context(read_only_knowledge=True):
        assert bool(command_denial_reason(command, tool=tool, cwd=str(tmp_path))) is shell
    direct = f"import subprocess; subprocess.run(['rm', '-rf', {target!r}], shell=False)"
    assert target in literal_paths(direct, python_source=True)[1]
