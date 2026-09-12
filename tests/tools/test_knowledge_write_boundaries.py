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


@pytest.mark.parametrize("command", [
    "printf x > cache/link/MEMORY.md",
    "printf x > 'cache/link/MEMORY.md'",
    "printf x > cache/link#alias/MEMORY.md",
    "cd cache; printf x > link/MEMORY.md",
    'python -c "open(\'cache/link/MEMORY.md\',\'w\').write(\'x\')"',
])
def test_literal_symlink_target_refused_before_terminal_dispatch(tmp_path, monkeypatch, command):
    from tools import terminal_tool as terminal
    home = tmp_path / "home"
    protected = home / "memories"
    protected.mkdir(parents=True)
    target = protected / "MEMORY.md"
    target.write_text("original")
    workspace = tmp_path / "work"
    (workspace / "cache").mkdir(parents=True)
    try:
        (workspace / "cache" / "link").symlink_to(protected, target_is_directory=True)
        (workspace / "cache" / "link#alias").symlink_to(protected, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Never execute even a disposable mutation in a red regression.
    monkeypatch.setattr(terminal, "_acquire_env", lambda *a: pytest.fail("reached execution"))
    with delegated_child_context(read_only_knowledge=True):
        result = json.loads(terminal.terminal_tool(command, workdir=str(workspace), task_id="alias-check"))
    assert "Parent-owned shared knowledge" in result["error"]
    assert target.read_text() == "original"


@pytest.mark.parametrize("source", [
    "rm -rf {home}", "/bin/rm --recursive --force -- {home}",
    "echo ready; rm -r {home}", "mv {home} displaced",
    "bash -lc 'rm -rf {home}'", "bash -cl 'rm -rf {home}'",
    'python -c "import shutil; shutil.rmtree({quoted})"',
])
def test_destructive_ancestor_refused_before_terminal_dispatch(tmp_path, monkeypatch, source):
    import shlex
    from tools import terminal_tool as terminal
    home = tmp_path / "home"
    protected = home / "memories"
    protected.mkdir(parents=True)
    (protected / "keep").write_text("original")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(terminal, "_acquire_env", lambda *a: pytest.fail("reached destructive execution"))
    command = source.format(home=shlex.quote(str(home)), quoted=repr(str(home)))
    with delegated_child_context(read_only_knowledge=True):
        result = json.loads(terminal.terminal_tool(command, workdir=str(tmp_path), task_id="ancestor-check"))
    assert "Parent-owned shared knowledge" in result["error"]
    assert (protected / "keep").read_text() == "original"


@pytest.mark.parametrize("source", [
    "import shutil; shutil.rmtree({home})",
    "from shutil import rmtree as wipe; wipe(path={home})",
    "import os; os.rename({home}, 'displaced')",
    "from pathlib import Path; Path({home}).rename('displaced')",
    "import subprocess; subprocess.run(args=['rm', '-rf', {home}])",
    "shell-literal",
])
def test_execute_code_destructive_literal_ancestor_never_spawns(tmp_path, monkeypatch, source):
    from tools import code_execution_tool as execute
    home = tmp_path / "home"
    protected = home / "memories"
    protected.mkdir(parents=True)
    (protected / "keep").write_text("original")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.code_kernel.execute_in_session_kernel", lambda *a, **k: pytest.fail("spawned code"))
    code = source.format(home=repr(str(home)))
    if source == "shell-literal":
        code = f"import os; os.system({('rm -rf ' + str(home))!r})"
    with delegated_child_context(read_only_knowledge=True):
        result = json.loads(execute.execute_code(code, task_id="ancestor-code"))
    assert "Parent-owned shared knowledge" in result["error"]
    assert (protected / "keep").read_text() == "original"


def test_ancestor_inspection_and_unrelated_mutations_stay_allowed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    commands = [f"ls {home}", f"cd {home}; printf x > ordinary.txt", f"echo 'rm -rf {home}'", f"ls {home} # note; rm -rf {home}",
                f"rm -rf {home}/unrelated", f"mv {home}/ordinary.txt {home}/other.txt"]
    with delegated_child_context(read_only_knowledge=True):
        for command in commands:
            assert knowledge_boundary.command_denial_reason(command, cwd=str(tmp_path)) is None, command
    assert knowledge_boundary.command_denial_reason(f"rm -rf {home}", cwd=str(tmp_path)) is None


@pytest.mark.parametrize("operation", ["write", "patch"])
@pytest.mark.parametrize("existing", [False, True])
def test_case_alias_existing_root_protects_existing_and_future_descendants(tmp_path, monkeypatch, operation, existing):
    home = tmp_path / "home"
    root = home / "memories"
    root.mkdir(parents=True)
    alias = home / "MEMORIES"
    if not alias.exists() or not alias.samefile(root):
        pytest.skip("fixture filesystem is case-sensitive")
    monkeypatch.setenv("HERMES_HOME", str(home))
    target = root / "nested" / "new.txt"
    if existing:
        target.parent.mkdir()
        target.write_text("original")
    path = alias / "nested" / "new.txt"
    with delegated_child_context(read_only_knowledge=True):
        if operation == "write":
            result = file_tools.write_file_tool(str(path), "bad", task_id="case-alias")
        elif existing:
            result = file_tools.patch_tool(path=str(path), old_string="original", new_string="bad", task_id="case-alias")
        else:
            result = file_tools.patch_tool(mode="patch", patch=f"*** Begin Patch\n*** Add File: {path}\n+bad\n*** End Patch", task_id="case-alias")
    assert "Parent-owned shared knowledge" in json.loads(result)["error"]
    assert target.read_text() == "original" if existing else not target.exists()


def test_distinct_case_sensitive_directory_is_not_protected(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "memories"
    root.mkdir(parents=True)
    sibling = home / "MEMORIES"
    if sibling.exists():
        pytest.skip("fixture filesystem is case-insensitive")
    sibling.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    with delegated_child_context(read_only_knowledge=True):
        result = json.loads(file_tools.write_file_tool(str(sibling / "new.txt"), "allowed", task_id="case-distinct"))
    assert not result.get("error")
    assert (sibling / "new.txt").read_text() == "allowed"


@pytest.mark.parametrize("source", [
    "import subprocess; subprocess.run(['rm', '-rf', {home}])",
    "import shutil as files; files.move(src={home}, dst='elsewhere')",
    "from os import replace as move; move({home}, 'elsewhere')",
])
def test_known_literal_python_destructive_wrappers_are_denied(tmp_path, monkeypatch, source):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    with delegated_child_context(read_only_knowledge=True):
        assert knowledge_boundary.command_denial_reason(source.format(home=repr(str(home))), tool="execute_code", cwd=str(tmp_path))


def test_shell_target_directory_move_preserves_unrelated_sibling_work(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    with delegated_child_context(read_only_knowledge=True):
        assert knowledge_boundary.command_denial_reason(f"mv -t {home} {tmp_path}/ordinary.txt", cwd=str(tmp_path)) is None
        assert knowledge_boundary.command_denial_reason(f"mv -t elsewhere {home}", cwd=str(tmp_path))


@pytest.mark.parametrize("source", [
    "import os; os.chdir({home}); open('memories/MEMORY.md', 'w').write('replacement')",
    "import os; os.chdir({home}); open('skills/x/SKILL.md', 'w').write('replacement')",
    "import os as o; o.chdir({home}); open('memories/MEMORY.md', 'w').write('x')",
    "from os import chdir; chdir(path={home}); open('memories/MEMORY.md', 'w').write('x')",
    "import os; from pathlib import Path; os.chdir(Path({home})); Path('memories/MEMORY.md').write_text('x')",
    # Relative transition from the workspace, then a relative operand.
    "import os; os.chdir('../home'); open('memories/MEMORY.md', 'w').write('x')",
    "import os; os.chdir('..'); os.chdir('home'); open('memories/MEMORY.md', 'w').write('x')",
])
def test_execute_code_literal_chdir_transition_is_tracked(tmp_path, monkeypatch, source):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    (home / "skills").mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    code = source.format(home=repr(str(home)))
    with delegated_child_context(read_only_knowledge=True):
        denial = knowledge_boundary.command_denial_reason(code, tool="execute_code", cwd=str(workspace))
    assert denial is not None and "Parent-owned shared knowledge" in denial
    assert knowledge_boundary.command_denial_reason(code, tool="execute_code", cwd=str(workspace)) is None


def test_execute_code_absolute_chdir_counts_without_known_cwd(tmp_path, monkeypatch):
    # The pre-spawn check has no cwd yet; an absolute literal transition still
    # gives later relative operands a base to resolve against.
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    code = f"import os; os.chdir({str(home)!r}); open('memories/MEMORY.md', 'w').write('x')"
    with delegated_child_context(read_only_knowledge=True):
        assert knowledge_boundary.command_denial_reason(code, tool="execute_code") is not None
        assert knowledge_boundary.command_denial_reason(
            f"import os; os.chdir({str(home)!r}); open('ordinary.txt', 'w').write('x')", tool="execute_code") is None


def test_execute_code_benign_chdir_keeps_unrelated_work_allowed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    benign = [
        f"import os; os.chdir({str(workspace / 'sub')!r}); open('memories.txt', 'w').write('x')",
        f"import os; os.chdir({str(home)!r}); open('ordinary.txt', 'w').write('x')",
        "import os; os.chdir('build'); open('out.log', 'w').write('x')",
        # Computed targets stay under the documented not-enforced contract
        # rather than turning every chdir into a refusal.
        "import os; os.chdir(os.getcwd()); open('out.log', 'w').write('x')",
    ]
    with delegated_child_context(read_only_knowledge=True):
        for code in benign:
            assert knowledge_boundary.command_denial_reason(code, tool="execute_code", cwd=str(workspace)) is None, code


@pytest.mark.parametrize("command", [
    # F5: the outer tokenization never sees the nested cd, the full protected
    # path never appears, and the destructive operand is relative to the
    # nested cwd, not the outer one.
    "bash -c 'cd {home}; rm -rf memories'",
    "sh -c 'cd {home}; rm -rf memories'",
    "zsh -c 'cd {home} && rm -rf memories'",
    "bash -c 'cd {home}; printf x > memories/MEMORY.md'",
    "sh -c 'cd -P -- {home}; rm -rf memories'",
    "sh -c 'builtin cd {home}; rm -rf memories'",
    "sh -c 'pushd {home}; rm -rf memories'",
    "python -c \"import os; os.chdir({quoted}); open('memories/x', 'w')\"",
    "python3 -c \"import os as o; o.chdir({quoted}); open('memories/x', 'w')\"",
    "python -c \"import shutil, os; os.chdir({quoted}); shutil.rmtree('memories')\"",
    # Relative nested transition from the workspace, and a double nesting.
    "bash -c 'cd ../home; rm -rf memories'",
    "bash -c \"sh -c 'cd {home}; rm -rf memories'\"",
    "python -c \"import os; os.system('cd {home}; rm -rf memories')\"",
    "python -c \"import subprocess; subprocess.run('cd {home}; rm -rf memories', shell=True)\"",
    "python -c \"import subprocess; subprocess.run(['rm', '-rf', 'memories'], cwd={quoted})\"",
])
def test_nested_literal_cd_transition_is_tracked(tmp_path, monkeypatch, command):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    command = command.format(home=str(home), quoted=repr(str(home)))
    with delegated_child_context(read_only_knowledge=True):
        denial = knowledge_boundary.command_denial_reason(command, cwd=str(workspace))
    assert denial is not None and "Parent-owned shared knowledge" in denial, command
    assert knowledge_boundary.command_denial_reason(command, cwd=str(workspace)) is None


@pytest.mark.parametrize("source", [
    "import os; os.system('cd {home}; rm -rf memories')",
    "import subprocess; subprocess.run('cd {home}; rm -rf memories', shell=True)",
    "import subprocess; subprocess.run(['bash', '-c', 'cd {home}; rm -rf memories'])",
    "import subprocess; subprocess.check_call(['rm', '-rf', 'memories'], cwd={quoted})",
    "exec(\"import os; os.chdir({quoted}); open('memories/x', 'w')\")",
])
def test_execute_code_nested_literal_cd_transition_is_tracked(tmp_path, monkeypatch, source):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    code = source.format(home=str(home), quoted=repr(str(home)))
    with delegated_child_context(read_only_knowledge=True):
        denial = knowledge_boundary.command_denial_reason(code, tool="execute_code", cwd=str(workspace))
    assert denial is not None and "Parent-owned shared knowledge" in denial, code


def test_nested_literal_cd_before_terminal_dispatch(tmp_path, monkeypatch):
    from tools import terminal_tool as terminal
    home = tmp_path / "home"
    protected = home / "memories"
    protected.mkdir(parents=True)
    (protected / "keep").write_text("original")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(terminal, "_acquire_env", lambda *a: pytest.fail("reached destructive execution"))
    with delegated_child_context(read_only_knowledge=True):
        result = json.loads(terminal.terminal_tool(
            f"bash -c 'cd {home}; rm -rf memories'", workdir=str(workspace), task_id="nested-cd"))
    assert "Parent-owned shared knowledge" in result["error"]
    assert (protected / "keep").read_text() == "original"


def test_nested_benign_cd_keeps_unrelated_work_allowed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    (workspace / "memories").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    benign = [
        # A nested cd to an unprotected directory, then the same operand name.
        f"bash -c 'cd {elsewhere}; rm -rf memories'",
        f"sh -c 'cd {workspace}; printf x > memories/notes.txt'",
        # The home itself is not protected; only its shared knowledge subtrees.
        f"bash -c 'cd {home}; printf x > ordinary.txt'",
        "bash -c 'cd build; rm -rf memories'",
        # Runtime-computed nested targets stay under the documented
        # not-enforced contract rather than becoming refusals.
        "bash -c 'cd \"$TARGET\"; rm -rf memories'",
        "bash -c 'cd -; rm -rf memories'",
        f"python -c \"import os; os.chdir({str(elsewhere)!r}); open('memories/x', 'w')\"",
    ]
    with delegated_child_context(read_only_knowledge=True):
        for command in benign:
            assert knowledge_boundary.command_denial_reason(command, cwd=str(workspace)) is None, command
