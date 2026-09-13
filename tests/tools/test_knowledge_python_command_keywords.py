"""Literal Python command APIs share the real admission guard; nothing executes."""
import shlex

import pytest

from agent.delegation_context import delegated_child_context
from tools.knowledge_boundary import command_denial_reason
from tools.knowledge_command_paths import literal_paths


@pytest.mark.parametrize("tool", ["execute_code", "terminal"])
@pytest.mark.parametrize("target_kind", ["home", "ancestor", "unrelated"])
@pytest.mark.parametrize("source", [
    "import os; os.popen({cmd}).read()",
    "import os; os.popen(cmd={cmd}).read()",
    "import os as o; o.popen(cmd={cmd}).read()",
    "from os import popen as p; p(cmd={cmd}).read()",
    "import subprocess as s; s.run(args={cmd}, shell=True)",
    "from subprocess import Popen as p; p(args={cmd}, shell=True)",
])
def test_literal_command_operands_protect_ancestors(tmp_path, monkeypatch, tool, target_kind, source):
    home = tmp_path / "parent" / "home"
    (home / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    targets = {"home": home, "ancestor": home.parent, "unrelated": tmp_path / "other"}
    target = str(targets[target_kind])
    code = source.format(cmd=repr("rm -rf " + shlex.quote(target)))
    command = code if tool == "execute_code" else "python -c " + shlex.quote(code)
    with delegated_child_context(read_only_knowledge=True):
        denial = command_denial_reason(command, tool=tool, cwd=str(tmp_path / "elsewhere"))
    if target_kind == "unrelated":
        assert denial is None
    else:
        assert denial and "Parent-owned shared knowledge" in denial
    assert literal_paths(command, python_source=tool == "execute_code")[1] == [target]


@pytest.mark.parametrize("tool", ["execute_code", "terminal"])
@pytest.mark.parametrize("nesting", ["shell-cd", "python-chdir", "subprocess-cwd"])
def test_nested_keyword_command_preserves_cwd_transitions(tmp_path, monkeypatch, tool, nesting):
    home = tmp_path / "parent" / "home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    if nesting == "shell-cd":
        code = f"import os; os.popen(cmd={('cd ' + shlex.quote(str(home.parent)) + '; rm -rf home')!r})"
    elif nesting == "python-chdir":
        code = f"import os as o; o.chdir({str(home.parent)!r}); o.popen(cmd='rm -rf home')"
    else:
        nested = "from os import popen as p; p(cmd='rm -rf home')"
        code = f"import subprocess as s; s.run(args=['python', '-c', {nested!r}], cwd={str(home.parent)!r})"
    command = code if tool == "execute_code" else "python -c " + shlex.quote(code)
    _, destructive, transitions = literal_paths(command, python_source=tool == "execute_code")
    assert destructive == ["home"]
    assert str(home.parent) in transitions
    with delegated_child_context(read_only_knowledge=True):
        denial = command_denial_reason(command, tool=tool, cwd=str(tmp_path / "elsewhere"))
    assert denial and "Parent-owned shared knowledge" in denial
