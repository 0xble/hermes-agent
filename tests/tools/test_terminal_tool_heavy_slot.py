"""Local terminal commands run their test suites and CI gates inside the machine-wide heavy-slot."""

from __future__ import annotations

import os
import stat
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools import terminal_tool_heavy_slot as heavy
from tools.terminal_tool_heavy_slot import classify_command, wrap_heavy_command

HELPER = "/opt/heavy/bin/heavy-slot"
LPG_COMMAND = (
    "env -u HEAVY_SLOT_HELD /Users/brianle/.local/bin/heavy-slot --label lpg-v2-fresh-copy-old-vs-batched-recovery "
    "-- bash /Users/brianle/.hermes/cache/scratch/lpg-v2-retry-proof-20261012/run-recovery.sh"
)
RESTART_SCRIPT = "#!/bin/sh\nhermes gateway restart\n"


def _lifecycle_blocked(result) -> bool:
    return isinstance(result, dict) and str(result.get("error", "")).startswith("Blocked:")


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def gateway_terminal(tmp_path, monkeypatch):
    """Drive the real ``terminal_tool`` entry point and lifecycle guard inside a supervised gateway.

    Only planning, env acquisition, approval and the final execution are stubbed; ``run(command)``
    returns the exact string handed to the shell, or the parsed blocked result.
    """
    import json
    from types import SimpleNamespace

    import tools.terminal_tool as terminal_module
    from tools import process_registry

    home = tmp_path / "home"
    installed = _executable(home / ".local" / "bin" / "heavy-slot", '#!/usr/bin/env python3\n"""/dev/null"""\n')
    shadow = _executable(tmp_path / "shadow" / "heavy-slot", RESTART_SCRIPT)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("PATH", os.pathsep.join([str(shadow.parent), str(installed.parent), "/usr/bin", "/bin"]))
    monkeypatch.setenv("HEAVY_SLOT_HELD", "1")  # keep wrap_heavy_command out of the executed string
    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr(terminal_module, "_PRE_EXEC_GUARD_MIN_TIMEOUT_S", 0)
    monkeypatch.setattr(
        terminal_module, "_run_approval_guards", lambda *_a, **_k: terminal_module._ApprovalVerdict(),
    )
    executed: list[str] = []

    def _foreground(command, *_a, execution_command=None, **_k):
        executed.append(execution_command or command)
        return "ran"

    monkeypatch.setattr(terminal_module, "_run_foreground", _foreground)

    def run(command: str, *, overrides: dict | None = None):
        plan = SimpleNamespace(
            config={}, env_type="local", effective_task_id="heavy-slot-pin", cwd=str(tmp_path),
            effective_timeout=30, promoted_from_foreground_timeout=None,
        )
        env = SimpleNamespace(cwd=str(tmp_path), host_cwd=None, env=dict(overrides or {}))
        monkeypatch.setattr(terminal_module, "_plan_execution", lambda *_a, **_k: plan)
        monkeypatch.setattr(terminal_module, "_acquire_env", lambda *_a, **_k: env)
        executed.clear()
        result = terminal_module.terminal_tool(command)
        return executed[-1] if result == "ran" else json.loads(result)

    return SimpleNamespace(run=run, installed=installed, shadow=shadow, tmp_path=tmp_path)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(("command", "expected"), [
    ("heavy-slot --label x -- true", "{h} --label x -- true"),
    ("  heavy-slot --label x -- true", "  {h} --label x -- true"),
    ("cd /tmp && heavy-slot --label x -- true", "cd /tmp && {h} --label x -- true"),
    ("true; heavy-slot --label x -- true", "true; {h} --label x -- true"),
    ("false || heavy-slot --label x -- true", "false || {h} --label x -- true"),
    ("true\nheavy-slot --label x -- true", "true\n{h} --label x -- true"),
    ("FOO=1 heavy-slot --label x -- true", "FOO=1 {h} --label x -- true"),
    ("env -u HEAVY_SLOT_HELD heavy-slot --label x -- true", "env -u HEAVY_SLOT_HELD {h} --label x -- true"),
])
def test_bare_top_level_heavy_slot_runs_the_installed_helper(gateway_terminal, command, expected):
    """The guard allows the command and the shell receives the absolute installed helper."""
    assert gateway_terminal.run(command) == expected.format(h=gateway_terminal.installed)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("prefix", ["", "export PATH={shadow}:$PATH; ", "PATH={shadow} ", "source {profile}; "])
def test_path_state_cannot_change_which_helper_runs(gateway_terminal, prefix):
    """A shadow earlier on PATH, a login-profile export or a prior PATH export never reaches the shell."""
    profile = gateway_terminal.tmp_path / "profile.sh"
    profile.write_text(f"export PATH={gateway_terminal.shadow.parent}:$PATH\n", encoding="utf-8")
    prefix = prefix.format(shadow=gateway_terminal.shadow.parent, profile=profile)
    executed = gateway_terminal.run(prefix + "heavy-slot --label x -- true")
    assert executed == prefix + f"{gateway_terminal.installed} --label x -- true"
    assert str(gateway_terminal.shadow) not in executed


@pytest.mark.platforms("posix")
def test_terminal_path_override_cannot_change_which_helper_runs(gateway_terminal):
    executed = gateway_terminal.run(
        "heavy-slot --label x -- true", overrides={"PATH": str(gateway_terminal.shadow.parent)},
    )
    assert executed == f"{gateway_terminal.installed} --label x -- true"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("command", [
    "heavy-slot --label x -- bash {restart}",
    "cd /tmp && heavy-slot --label x -- bash {restart}",
    "heavy-slot --label x -- {restart}",
])
def test_bare_heavy_slot_wrapping_a_restart_is_blocked(gateway_terminal, command):
    restart = _executable(gateway_terminal.tmp_path / "restart.sh", RESTART_SCRIPT)
    result = gateway_terminal.run(command.format(restart=restart))
    assert _lifecycle_blocked(result)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("command", [
    "bash -c 'heavy-slot --label x -- true'",
    'sh -c "heavy-slot --label x -- true"',
    "env -S 'heavy-slot --label x -- true'",
    "(heavy-slot --label x -- true)",
    "echo $(heavy-slot --label x -- true)",
    "true | heavy-slot --label x -- true",
])
def test_nested_and_unpinned_bare_heavy_slot_stays_strict(gateway_terminal, command):
    """Not rewritten; the strict walk reads the PATH-shadowing helper and refuses the command."""
    result = gateway_terminal.run(command)
    assert _lifecycle_blocked(result)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("command", [
    "echo heavy-slot --label x -- true",
    "echo 'heavy-slot --label x -- true'",
    "'heavy-slot' --label x -- true",
    "cat <<'EOF'\nheavy-slot --label x -- true\nEOF",
    "heavy-slot() { true; }",
    "./heavy-slot --label x -- true",
    "command -v heavy-slot",
    "eval 'heavy-slot --label x -- true'",
    "env -i heavy-slot --label x -- true",
])
def test_non_command_word_heavy_slot_text_is_not_rewritten(command, tmp_path, monkeypatch):
    home = tmp_path / "home"
    _executable(home / ".local" / "bin" / "heavy-slot", "#!/bin/sh\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    assert heavy.pin_bare_heavy_slot(command, env_type="local") == command


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("env_type", ["docker", "ssh", "modal"])
def test_non_local_backends_are_not_pinned(tmp_path, monkeypatch, env_type):
    home = tmp_path / "home"
    _executable(home / ".local" / "bin" / "heavy-slot", "#!/bin/sh\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    command = "heavy-slot --label x -- true"
    assert heavy.pin_bare_heavy_slot(command, env_type=env_type) == command


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("mode", [None, 0o644])
def test_missing_or_non_executable_helper_is_not_pinned(tmp_path, monkeypatch, mode):
    home = tmp_path / "home"
    if mode is not None:
        _executable(home / ".local" / "bin" / "heavy-slot", "#!/bin/sh\n").chmod(mode)
    monkeypatch.setattr(Path, "home", lambda: home)
    command = "heavy-slot --label x -- true"
    assert heavy.pin_bare_heavy_slot(command, env_type="local") == command


@pytest.mark.platforms("posix")
def test_lpg_absolute_command_is_allowed_through_the_local_terminal(gateway_terminal, monkeypatch):
    """Locally the remote-read callback is not passed, so absolute identity peeling still applies."""
    from cron.lifecycle_guard import scan_gateway_lifecycle

    home = gateway_terminal.installed.parents[2]
    command = LPG_COMMAND.replace("/Users/brianle/.local/bin/heavy-slot", str(gateway_terminal.installed))
    _executable(home / "run-recovery.sh", "#!/bin/sh\nprintf 'proof\\n'\n")
    command = command.replace(
        "/Users/brianle/.hermes/cache/scratch/lpg-v2-retry-proof-20261012/run-recovery.sh", str(home / "run-recovery.sh"),
    )
    assert scan_gateway_lifecycle(command) == (False, None)
    assert gateway_terminal.run(command) == command


@pytest.fixture
def helper(monkeypatch):
    monkeypatch.delenv("HEAVY_SLOT_HELD", raising=False)  # the suite itself may run inside a slot
    monkeypatch.delenv("HERMES_HEAVY_SLOT", raising=False)
    monkeypatch.setattr(heavy, "_heavy_slot_executable", lambda _env: HELPER)


@pytest.mark.parametrize(("command", "label"), [
    ("pytest -q tests", "pytest"),
    ("node scripts/run-workspace-checks.mjs", "workspace-checks"),
    ("npx eslint .", "eslint"),
    ("npx tsc --noEmit", "tsc"),
    ("npm ci", "install"),
    ("pip install -r requirements.txt", "pip-install"),
    ("uv sync --compile-bytecode", "uv-sync"),
    ("python -m unittest discover", "unittest"),
    ("agentkit evals admit --trials 12", "evals-admit"),
    ("agentkit evals run --trials 12", "evals-run"),
    ("./bin/check --fast", "check"),
    ("node viewer/check.js", "check"),
    ("python pm/launch.py install --test-environment", "pm-install"),
    ("go test ./...", "go-test"),
    ("scripts/run_tests.sh tests/agent/test_foo.py -k test_x", "run_tests"),
    ("python scripts/run_tests_parallel.py tests/test_a.py", "run_tests"),
    ("python -m compileall -j 4 .", "compileall"),
    ("python -m pip install pytest", "pip-install"),
    ("uv pip install pytest", "uv-install"),
    ("pnpm build", "build"),
    ("turbo run build", "build"),
    ("pnpm --filter test build", "build"),
    ("npm install test", "install"),
    ("pnpm add -D vitest", "install"),
    ("yarn workspace app add -D test", "install"),
    ("npm run check", "check"),
    ("npx playwright test", "playwright"),
    ("python -m pytest tests/", "pytest"),
    (".venv/bin/python -m pytest -n 8 tests/tools/test_a.py", "pytest"),
    ("cd repo && uv run pytest -q", "pytest"),
    ("uv run --frozen pytest tests/test_store.py tests/x/", "pytest"),
    ("scripts/run_tests.sh", "run_tests"),
    ("bash scripts/run_tests.sh tests/gateway/", "run_tests"),
    ("vitest run", "vitest"),
    ("npx vitest", "vitest"),
    ("pnpm test", "test"),
    ("pnpm --filter app test:unit", "test"),
    ("npm run test", "test"),
    ("turbo run test --filter=app", "test"),
    ("yarn workspace app test", "test"),
    ("yarn workspaces foreach run test", "test"),
    ("npm -w packages/app run test:unit", "test"),
    ("pnpm -r test", "test"),
    ("nx run-many -t test", "test"),
    ("lerna run test", "test"),
    ("tox -e py311", "tox"),
    ("CI_EXPECTED_SHA=abc ./bin/ci gate deadbeef", "ci-gate"),
    ("./bin/ci", "ci-full"),
    ("bash -c 'cd x && pytest'", "pytest"),
    ("if true; then pytest; fi", "pytest"),
    ("env -u FOO PYTHONPATH=. timeout 600 pytest tests", "pytest"),
    # Targeted file runs that may still fan out keep the slot (unknown options fail toward it).
    ("pytest -n 8 tests/tools/test_a.py", "pytest"),
    ("pytest --numprocesses=auto tests/tools/test_a.py", "pytest"),
    ("jest --maxWorkers=4 src/a.test.js", "jest"),
    ("jest -w 8", "jest"),
    ("vitest --pool=threads src/a.test.ts", "vitest"),
    ("vitest run --maxWorkers 4 src/a.test.ts", "vitest"),
    ("vitest --watch=false", "vitest"),
    ("vitest run --watch=false", "vitest"),
    ("jest --watch=false", "jest"),
    ("pnpm test --watch=false", "test"),
    ("scripts/run_tests.sh --workers 8 tests/agent/test_foo.py", "run_tests"),
])
def test_recognizes_heavy_commands(command, label, helper):
    assert classify_command(command) == label
    assert f"hermes {label}" in wrap_heavy_command(command, env_type="local")


@pytest.mark.parametrize("command", [
    "git status",
    "ls",
    "cat scripts/run-workspace-checks.mjs",
    "rg eslint .",
    "node viewer/serve.js",
    "agentkit evals list",
    "go version",
    "python pm/launch.py status",
    "uv --version",
    "uv run --frozen python -c 'print(1)'",
    "grep -rn pytest .",
    "rg 'pytest -q' tools",
    "echo 'a; pytest b'",
    "printf '%s' pytest",
    "git log --grep='x && pytest -q'",
    "git commit -m 'run pytest -q'",
    "ls # pytest",
    "python - <<'PY'\nprint('pytest -q tests')\nPY",
    "cat <<EOF > notes.md\npytest -q tests\nEOF",
    "./bin/ci preflight",
    "~/.hermes/plugins/git-guard/bin/ci-gate deadbeef",  # ci-gate takes its own slot
    "pytest tests/tools/test_a.py tests/tools/test_b.py -q",  # targeted
    "vitest run src/a.test.ts",  # targeted
    "pytest -q -x --tb=short -k smoke tests/tools/test_a.py::test_b",  # targeted with single-run options
    "command pytest tests",  # `command` deliberately bypasses shell functions: left alone
    "exec pytest tests",  # would replace the session shell: left alone
])
def test_leaves_light_commands_alone(command):
    assert classify_command(command) is None


@pytest.mark.parametrize(("command", "label"), [
    ("tsc --watch", None),
    ("tsc -b --watch", None),
    ("tsc -w", None),
    ("vitest --watch", None),
    ("vitest -w", None),
    ("jest --watch", None),
    ("pnpm build --watch", None),
    ("npm run build:watch", None),
    ("pnpm watch", None),
    ("turbo run watch", None),
    ("tsc --version", None),
    ("vitest --help", None),
    ("jest -h", None),
    ("pytest --version", None),
    ("./bin/ci --help", None),
    ("pnpm build", "build"),
])
def test_watch_info_and_non_watch_build_classification(command, label, helper):
    assert classify_command(command) == label
    wrapped = wrap_heavy_command(command, env_type="local")
    if label is None:
        assert wrapped == command
    else:
        assert f"hermes {label}" in wrapped


def test_routes_only_the_heavy_simple_command(helper):
    wrapped = wrap_heavy_command("cd repo && export X=1 && pytest -q tests; echo done", env_type="local")
    dispatcher, body = wrapped.split("\n", 1)
    assert dispatcher.startswith("_hermes_heavy_slot() {") and HELPER in dispatcher
    assert body == "cd repo && export X=1 && _hermes_heavy_slot 'hermes pytest' pytest -q tests; echo done"


@pytest.mark.parametrize(("command", "expected"), [
    ("env -u FOO PYTHONPATH=. timeout 600 pytest tests",
     "_hermes_heavy_slot 'hermes pytest' env -u FOO PYTHONPATH=. timeout 600 pytest tests"),
    ("FOO=1 nice -n 5 pytest", "FOO=1 _hermes_heavy_slot 'hermes pytest' nice -n 5 pytest"),
    ("nohup pnpm test", "_hermes_heavy_slot 'hermes test' nohup pnpm test"),
    ("time pytest tests", "time _hermes_heavy_slot 'hermes pytest' pytest tests"),
])
def test_exec_prefix_utilities_run_inside_the_dispatcher(helper, command, expected):
    """env/nice/timeout/nohup exec their argument and cannot run a shell function."""
    assert wrap_heavy_command(command, env_type="local").split("\n", 1)[1] == expected


def test_routes_each_heavy_command_once(helper):
    body = wrap_heavy_command("pnpm test && ./bin/ci gate abc", env_type="local").split("\n", 1)[1]
    assert body == "_hermes_heavy_slot 'hermes test' pnpm test && _hermes_heavy_slot 'hermes ci-gate' ./bin/ci gate abc"


@pytest.mark.parametrize("env_type", ["docker", "ssh", "modal", "managed_modal", "singularity", "daytona"])
def test_non_local_backends_are_not_wrapped(helper, env_type):
    assert wrap_heavy_command("pytest -q tests", env_type=env_type) == "pytest -q tests"


@pytest.mark.parametrize("environment", [{"HEAVY_SLOT_HELD": "1"}, {"HERMES_HEAVY_SLOT": "off"},
                                         {"HERMES_HEAVY_SLOT": "0"}])
def test_nested_slot_and_opt_out_are_not_wrapped(helper, environment):
    assert wrap_heavy_command("pytest -q tests", env_type="local", environment=environment) == "pytest -q tests"


def test_process_env_opt_out_and_missing_helper(helper, monkeypatch):
    monkeypatch.setenv("HERMES_HEAVY_SLOT", "off")
    assert wrap_heavy_command("pytest", env_type="local") == "pytest"
    monkeypatch.delenv("HERMES_HEAVY_SLOT")
    monkeypatch.setattr(heavy, "_heavy_slot_executable", lambda _env: None)
    assert wrap_heavy_command("pytest", env_type="local") == "pytest"


def test_classifier_failure_runs_the_command_unwrapped(helper, monkeypatch):
    def boom(_command):
        raise RuntimeError("parser bug")
    monkeypatch.setattr(heavy, "_heavy_positions", boom)
    assert wrap_heavy_command("pytest", env_type="local") == "pytest"


def _fake_helper(tmp_path: Path) -> str:
    """A stand-in heavy-slot: records its argv and runs the command with HEAVY_SLOT_HELD set."""
    log = tmp_path / "helper.log"
    script = tmp_path / "heavy-slot"
    script.write_text(
        "#!/bin/sh\n"
        f"echo \"$@\" >> {log}\n"
        "while [ \"$1\" != -- ]; do shift; done; shift\n"
        "HEAVY_SLOT_HELD=1 exec \"$@\"\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_dispatcher_leaves_shell_functions_and_aliases_to_the_shell(tmp_path, monkeypatch, shell):
    """A user's own pytest function or ./bin/ci alias runs as before; external programs take the slot."""
    if not shutil.which(shell):
        pytest.skip(f"{shell} not installed")
    monkeypatch.delenv("HEAVY_SLOT_HELD", raising=False)
    monkeypatch.delenv("HERMES_HEAVY_SLOT", raising=False)
    fake = _fake_helper(tmp_path)
    monkeypatch.setattr(heavy, "_heavy_slot_executable", lambda _env: fake)
    runner = tmp_path / "vitest"
    runner.write_text("#!/bin/sh\necho \"external $* held=$HEAVY_SLOT_HELD\"\n")
    runner.chmod(runner.stat().st_mode | stat.S_IXUSR)
    command = wrap_heavy_command(
        f"pytest -q 'a b' && FOO=1 tox -e x && {runner} run 'c d' && env BAR=2 timeout 30 {runner} --all",
        env_type="local")
    setup = ("pytest() { echo \"function $* FOO=$FOO\"; }\n"
             "tox() { echo \"tox-fn FOO=$FOO\"; }\n")
    if shell == "bash":
        setup = "shopt -s expand_aliases\n" + setup
    out = subprocess.run([shell, "-c", setup + command], capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        "function -q a b FOO=", "tox-fn FOO=1", "external run c d held=1", "external --all held=1"]
    assert (tmp_path / "helper.log").read_text().count("--label") == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
def test_wrapped_command_keeps_shell_state_and_runs_in_slot(tmp_path, monkeypatch):
    """cd and exports around the heavy command still reach the session shell."""
    from tools.environments.local import LocalEnvironment

    monkeypatch.delenv("HEAVY_SLOT_HELD", raising=False)
    monkeypatch.delenv("HERMES_HEAVY_SLOT", raising=False)
    fake = _fake_helper(tmp_path)
    monkeypatch.setattr(heavy, "_heavy_slot_executable", lambda _env: fake)
    (tmp_path / "sub").mkdir()
    runner = tmp_path / "pytest"
    runner.write_text("#!/bin/sh\necho \"held=$HEAVY_SLOT_HELD\"\n")
    runner.chmod(runner.stat().st_mode | stat.S_IXUSR)

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        command = f"cd sub && export HS_PROBE=kept && {runner} -q"
        wrapped = wrap_heavy_command(command, env_type="local", environment=env.env)
        assert wrapped != command
        result = env.execute(wrapped)
        assert result["returncode"] == 0, result
        assert "held=1" in result["output"]
        assert env.cwd.endswith("/sub")
        assert "kept" in env.execute("echo $HS_PROBE")["output"]
    finally:
        env.cleanup()
    assert (tmp_path / "helper.log").read_text().startswith("--label hermes pytest (in ")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
@pytest.mark.live_system_guard_bypass  # kills the heavy-slot tree it started itself
def test_background_wrapped_run_is_killable_and_releases_its_slot(tmp_path, monkeypatch):
    """The real helper under the process registry: kill stops the whole tree and frees the slot."""
    real = Path.home() / ".local" / "bin" / "heavy-slot"
    if not os.access(real, os.X_OK):
        pytest.skip("heavy-slot helper not installed")
    from tools.process_registry import ProcessRegistry

    monkeypatch.delenv("HEAVY_SLOT_HELD", raising=False)
    monkeypatch.delenv("HERMES_HEAVY_SLOT", raising=False)
    slots = tmp_path / "slots"
    monkeypatch.setenv("HEAVY_SLOT_DIR", str(slots))  # private slots: never touch the host's
    monkeypatch.setenv("HEAVY_SLOT_STOP_GRACE", "1")
    monkeypatch.setattr(ProcessRegistry, "_daemon_term_grace_seconds", staticmethod(lambda: 1.0))
    monkeypatch.setattr(ProcessRegistry, "_write_checkpoint", lambda self: None)
    runner = tmp_path / "pytest"
    runner.write_text("#!/bin/sh\necho started held=$HEAVY_SLOT_HELD\nexec sleep 60\n")
    runner.chmod(runner.stat().st_mode | stat.S_IXUSR)

    command = wrap_heavy_command(f"{runner} tests/", env_type="local")
    assert str(real) in command
    reg = ProcessRegistry()
    session = reg.spawn_local(command, cwd=str(tmp_path), env_vars={"HEAVY_SLOT_DIR": str(slots)})
    try:
        deadline = time.monotonic() + 20
        while "started" not in session.output_buffer and time.monotonic() < deadline:
            time.sleep(0.1)
        assert "started held=0" in session.output_buffer or "started held=1" in session.output_buffer
        result = reg.kill_process(session.id)
        assert result["status"] == "killed", result
    finally:
        subprocess.run(["pkill", "-f", str(runner)], check=False)

    deadline = time.monotonic() + 10
    while subprocess.run(["pgrep", "-f", str(runner)], capture_output=True).returncode == 0 \
            and time.monotonic() < deadline:
        time.sleep(0.2)
    assert subprocess.run(["pgrep", "-f", str(runner)], capture_output=True).returncode != 0
    deadline = time.monotonic() + 10
    while any(p.stat().st_size for p in slots.glob("slot-*.lock")) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not any(p.stat().st_size for p in slots.glob("slot-*.lock"))


def test_helper_lookup_prefers_terminal_path_then_home_bin(tmp_path, monkeypatch):
    on_path = tmp_path / "path-bin"
    home_bin = tmp_path / "home" / ".local" / "bin"
    for directory in (on_path, home_bin):
        directory.mkdir(parents=True)
        helper = directory / "heavy-slot"
        helper.write_text("#!/bin/sh\n")
        helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert heavy._heavy_slot_executable({"PATH": str(on_path)}) == str(on_path / "heavy-slot")
    assert heavy._heavy_slot_executable({"PATH": str(tmp_path / "empty")}) == str(home_bin / "heavy-slot")
    (home_bin / "heavy-slot").unlink()
    assert heavy._heavy_slot_executable({"PATH": str(tmp_path / "empty")}) is None
