"""Compare the real command guard with Bash argv, never with real deletion."""
import json
import os
import shlex
import subprocess

import pytest

from agent.delegation_context import delegated_child_context
from tools.knowledge_boundary import command_denial_reason


@pytest.mark.parametrize("operand,denied", [
    ("{home}/*", True),
    ("{home}/m*", True),
    ("~/active-home/m*", True),
    ("'~'/active-home/m*", False),
    ("{home}/m\\\n*", True),
    ("{home}/s[k]ills", True),
    ("{home}/s[^a]ills", True),
    ("{home}/s[[:alpha:]]ills", True),
    ("{home}/'s[k]'*", False),
    (r"{home}/s\[k\]*", False),
    ("{project}/hidden/[.]alias", False),
    ("{project}/ordinary/*", False),
    ("{home}/memor?es", True),
    ("'{home}/*'", False),
    ('"{home}/*"', False),
    (r"{home}/\*", False),
    ('"{home}/"m*', True),
    ("{home}/'m'*", True),
    ("{project}/empty/*", False),
    ("{project}/../active*/m*", True),
    ("{project}/alias/m*", True),
    ("{project}/ali*", True),
    ("{project}/empty/*/../../active-home/m*", False),
    ("{project}/hidden/*", False),
    ("{project}/hidden/.*", True),
])
def test_destructive_glob_guard_matches_shell_targets(tmp_path, monkeypatch, operand, denied):
    home = tmp_path / "active-home"
    project = tmp_path / "project"
    for path in (home / "memories", home / "skills", project / "empty", project / "hidden", project / "ordinary"):
        path.mkdir(parents=True)
    (project / "alias").symlink_to(home, target_is_directory=True)
    (project / "hidden" / ".alias").symlink_to(home, target_is_directory=True)
    (project / "ordinary" / "file").touch()
    sentinel = home / "memories" / "sentinel"
    sentinel.write_text("untouched")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    command = "rm -rf -- " + operand.format(home=home, project=project)
    # env -i equivalent; no user rc, BASH_ENV, external rm or filesystem writes.
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c",
         "rm() { printf '%s\\0' \"$@\"; }; " + command],
        cwd=project, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LC_ALL": "C"},
        capture_output=True, check=True,
    )
    targets = result.stdout.decode().split("\0")[2:-1]
    targets = [os.path.join(project, target) for target in targets]
    reaches_home = any(
        os.path.realpath(home / "memories") == os.path.realpath(target)
        or os.path.realpath(home / "memories").startswith(os.path.realpath(target).rstrip("/") + "/")
        or os.path.realpath(home / "skills") == os.path.realpath(target)
        for target in targets
    )
    assert reaches_home is denied, targets
    with delegated_child_context(read_only_knowledge=True):
        reason = command_denial_reason(command, cwd=str(project))
        if denied:
            from tools import terminal_tool

            monkeypatch.setattr(terminal_tool, "_plan_execution", lambda *a, **kw: pytest.fail("must deny before planning"))
            result = json.loads(terminal_tool.terminal_tool(command, workdir=str(project)))
            assert "Parent-owned shared knowledge" in result["error"]
    assert bool(reason) is denied, (command, reason, targets)
    assert sentinel.read_text() == "untouched"


# Interpreter source below is scanner input only, never executed.
@pytest.mark.parametrize("wrapper,shell_expands", [
    ("cd {home}; rm -rf m*", True),
    ("bash -c {body}", True),
    ("import os; os.system({body!r})", True),
    ("import subprocess; subprocess.run(['rm', '-rf', {pattern!r}])", False),
])
def test_glob_provenance_survives_nested_shell_but_not_literal_argv(
    tmp_path, monkeypatch, wrapper, shell_expands,
):
    home = tmp_path / "active-home"
    (home / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    body = f"cd {home}; rm -rf m*"
    python_source = wrapper.startswith("import")
    command = wrapper.format(home=home, body=body if python_source else shlex.quote(body), pattern=str(home / "m*"))
    with delegated_child_context(read_only_knowledge=True):
        reason = command_denial_reason(command, cwd=str(tmp_path), tool="execute_code" if python_source else "terminal")
    assert bool(reason) is shell_expands
