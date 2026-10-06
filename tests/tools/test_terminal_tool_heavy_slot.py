"""Local terminal commands run their test suites and CI gates inside the machine-wide heavy-slot."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools import terminal_tool_heavy_slot as heavy
from tools.terminal_tool_heavy_slot import classify_command, wrap_heavy_command

HELPER = "/opt/heavy/bin/heavy-slot"


@pytest.fixture
def helper(monkeypatch):
    monkeypatch.delenv("HEAVY_SLOT_HELD", raising=False)  # the suite itself may run inside a slot
    monkeypatch.delenv("HERMES_HEAVY_SLOT", raising=False)
    monkeypatch.setattr(heavy, "_heavy_slot_executable", lambda _env: HELPER)


@pytest.mark.parametrize(("command", "label"), [
    ("pytest -q tests", "pytest"),
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
    ("vitest --pool=threads src/a.test.ts", "vitest"),
    ("vitest run --maxWorkers 4 src/a.test.ts", "vitest"),
    ("scripts/run_tests.sh --workers 8 tests/agent/test_foo.py", "run_tests"),
])
def test_recognizes_heavy_commands(command, label):
    assert classify_command(command) == label


@pytest.mark.parametrize("command", [
    "git status",
    "grep -rn pytest .",
    "rg 'pytest -q' tools",
    "echo 'a; pytest b'",
    "printf '%s' pytest",
    "git log --grep='x && pytest -q'",
    "git commit -m 'run pytest -q'",
    "ls # pytest",
    "python - <<'PY'\nprint('pytest -q tests')\nPY",
    "cat <<EOF > notes.md\npytest -q tests\nEOF",
    "uv pip install pytest",
    "pnpm add -D vitest",
    "pnpm build",
    "./bin/ci preflight",
    "~/.hermes/plugins/git-guard/bin/ci-gate deadbeef",  # ci-gate takes its own slot
    "pytest tests/tools/test_a.py tests/tools/test_b.py -q",  # targeted
    "scripts/run_tests.sh tests/agent/test_foo.py -k test_x",  # targeted
    "vitest run src/a.test.ts",  # targeted
    "pytest -q -x --tb=short -k smoke tests/tools/test_a.py::test_b",  # targeted with single-run options
])
def test_leaves_light_commands_alone(command):
    assert classify_command(command) is None


def test_prefixes_only_the_heavy_simple_command(helper):
    wrapped = wrap_heavy_command("cd repo && export X=1 && pytest -q tests; echo done", env_type="local")
    assert wrapped == (
        "cd repo && export X=1 && "
        f"{HELPER} --label 'hermes pytest'\" (in $PWD)\" -- pytest -q tests; echo done"
    )


def test_wraps_each_heavy_command_once(helper):
    wrapped = wrap_heavy_command("pnpm test && ./bin/ci gate abc", env_type="local")
    assert wrapped.count(HELPER) == 2
    assert wrapped.index("pnpm test") < wrapped.index("./bin/ci gate")


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
