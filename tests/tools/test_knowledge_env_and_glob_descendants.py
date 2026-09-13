"""Exercise literal command boundaries without executing destructive commands."""
import shlex

import pytest

from agent.delegation_context import delegated_child_context
from tools.knowledge_boundary import command_denial_reason


@pytest.mark.parametrize("quoted", [False, True])
def test_expanded_glob_descendant_is_protected(tmp_path, monkeypatch, quoted):
    home = tmp_path / "users" / "alice" / ".hermes"
    protected = home / "memories" / "MEMORY.md"
    protected.parent.mkdir(parents=True)
    protected.write_text("unchanged")
    monkeypatch.setenv("HERMES_HOME", str(home))
    pattern = str(tmp_path / "users" / "*" / ".hermes" / "memories" / "MEMORY.md")
    operand = shlex.quote(pattern) if quoted else pattern
    with delegated_child_context(read_only_knowledge=True):
        reason = command_denial_reason("rm -rf " + operand, cwd=str(tmp_path / "elsewhere"))
    assert bool(reason) is not quoted
    assert protected.read_text() == "unchanged"


@pytest.mark.parametrize("option", ["-C {path}", "--chdir {path}", "--chdir={path}", "-C{path}"])
@pytest.mark.parametrize("protected", [True, False])
def test_env_directory_governs_nested_python(tmp_path, monkeypatch, option, protected):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    destination = home if protected else tmp_path / "ordinary"
    code = "open('memories/MEMORY.md', 'w').write('changed')"
    command = "env -u UNUSED " + option.format(path=shlex.quote(str(destination)))
    command += " MODE=test python -c " + shlex.quote(code)
    with delegated_child_context(read_only_knowledge=True):
        reason = command_denial_reason(command, cwd=str(tmp_path / "elsewhere"))
    assert bool(reason) is protected
