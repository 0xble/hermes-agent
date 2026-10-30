"""Queued work keeps its original shell and admission ticket across background handoff."""

import json
import os
import shlex
import sys

import pytest

from tools import terminal_tool, terminal_tool_heavy_slot as heavy
from tools.environments.local import LocalEnvironment
from tools.process_registry import process_registry
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture(autouse=True)
def source_checkout_env(tmp_path, monkeypatch):
    # These shell tests do not run from a sealed payload. Keep package discovery
    # out of the source checkout's parent, which may be inside the real home.
    monkeypatch.setattr("pm.paths.repo_root", lambda: tmp_path / "source")
    monkeypatch.setattr("pm.environments._payload_manifest", lambda _: None)


def _executable(path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(("compound", "gate"), [(False, False), (True, False), (False, True)])
def test_waiting_process_is_adopted_once_with_its_ticket(tmp_path, monkeypatch, compound, gate):
    monkeypatch.delenv("HEAVY_SLOT_HELD", raising=False)
    monkeypatch.delenv("HERMES_HEAVY_SLOT", raising=False)
    ticket = tmp_path / "ticket"
    release = tmp_path / "admit"
    os.mkfifo(release)
    # An open read/write endpoint lets the test release the helper without a
    # blocking writer open. The helper itself waits for this explicit admission.
    fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    helper = _executable(tmp_path / "heavy-slot", f'''#!{sys.executable}
import os, sys
from pathlib import Path
assert not os.environ.get("HEAVY_SLOT_MAX_WAIT")
assert sys.argv[sys.argv.index("--label") + 1].startswith("bin/ci gate " + "a" * 40) is {gate!r}
with Path({str(ticket)!r}).open("a") as stream:
    stream.write(str(os.getpid()) + "\\n")
with open({str(release)!r}, "rb", buffering=0) as stream:
    stream.read(1)
args = sys.argv[sys.argv.index("--") + 1:]
os.execvp(args[0], args)
''')
    runner = _executable(tmp_path / "pytest", "#!/bin/sh\nprintf 'completed\\n'\n")
    monkeypatch.setattr(heavy, "_heavy_slot_executable", lambda _: str(helper))
    monkeypatch.setattr(terminal_tool, "_heavy_slot_max_wait_for", lambda **_: 0)
    env = LocalEnvironment(cwd=str(tmp_path), env={"PATH": f"{tmp_path}:{os.environ['PATH']}"})
    monkeypatch.setattr(terminal_tool, "_acquire_env", lambda *_: env)
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {
        "env_type": "local", "cwd": str(tmp_path), "timeout": 30, "host_cwd": None,
    })
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda **_: {"approved": True})
    # The real process is already running. Re-spawning would lose ticket identity
    # and replay the shell's earlier side effect.
    monkeypatch.setattr(terminal_tool, "spawn_background_process", lambda **_: pytest.fail("must adopt, not respawn"))
    before = tmp_path / "before"
    if gate:
        _executable(tmp_path / "bin/ci", "#!/bin/sh\nprintf 'completed\\n'\n")
        command = "./bin/ci gate " + "a" * 40
    else:
        command = f"{shlex.quote(str(runner))} -q tests"
    if compound:
        command = f"printf 'once\\n' >> {shlex.quote(str(before))}; {command}; printf 'after\\n'"
    session_id = None
    try:
        result = json.loads(terminal_tool.terminal_tool(command, timeout=30, task_id="ticket-handoff", force=True))
        assert result.get("status") == "yielded_to_background", result
        assert result["notify_on_complete"] is True
        assert "still queued for a heavy slot" in result["note"]
        assert "same shell and ticket" in result["note"]
        assert "notified when it exits" in result["note"]
        assert "user sent a message" not in result["note"].lower()
        session_id = result["session_id"]
        assert wait_until(ticket.exists, timeout=5)
        original = ticket.read_text()
        assert len(original.splitlines()) == 1
        assert process_registry.poll(session_id)["status"] == "running"
        os.write(fd, b"x")
        completed = process_registry.wait(session_id, timeout=10)
        assert completed["exit_code"] == 0, completed
        assert "completed" in completed["output"]
        assert ticket.read_text() == original
        if compound:
            assert before.read_text() == "once\n"
            assert "after" in completed["output"]
    finally:
        os.close(fd)
        if session_id and process_registry.poll(session_id)["status"] == "running":
            process_registry.kill_process(session_id)
        env.cleanup()


def test_admitted_process_keeps_its_foreground_deadline(tmp_path, monkeypatch):
    monkeypatch.delenv("HEAVY_SLOT_HELD", raising=False)
    monkeypatch.delenv("HERMES_HEAVY_SLOT", raising=False)
    started = tmp_path / "started"
    release = tmp_path / "release"
    os.mkfifo(release)
    fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    helper = _executable(tmp_path / "heavy-slot", "#!/bin/sh\nwhile [ \"$1\" != -- ]; do shift; done; shift; exec \"$@\"\n")
    runner = _executable(tmp_path / "pytest", f'''#!{sys.executable}
from pathlib import Path
Path({str(started)!r}).touch()
with open({str(release)!r}, "rb", buffering=0) as stream:
    stream.read(1)
print("completed")
''')
    monkeypatch.setattr(heavy, "_heavy_slot_executable", lambda _: str(helper))
    monkeypatch.setattr(terminal_tool, "_heavy_slot_max_wait_for", lambda **_: 0)
    env = LocalEnvironment(cwd=str(tmp_path))
    wait_for_process = env._wait_for_process
    checked = []

    def wait_after_admission(*args, **kwargs):
        # Synchronize admission before the budget starts, rather than betting
        # that a subprocess gets CPU time before a wall-clock threshold.
        assert wait_until(started.exists, timeout=5)
        waiting = kwargs["yield_when"]

        def check_admission():
            pending = waiting()
            checked.append(pending)
            os.write(fd, b"x")
            return pending

        kwargs["yield_when"] = check_admission
        return wait_for_process(*args, **kwargs)

    monkeypatch.setattr(env, "_wait_for_process", wait_after_admission)
    monkeypatch.setattr(terminal_tool, "_acquire_env", lambda *_: env)
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {
        "env_type": "local", "cwd": str(tmp_path), "timeout": 30, "host_cwd": None,
    })
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda **_: {"approved": True})
    monkeypatch.setattr(terminal_tool, "yield_to_background_handler",
                        lambda **_: lambda *_: pytest.fail("running work must stay foreground"))
    try:
        result = json.loads(terminal_tool.terminal_tool(f"{shlex.quote(str(runner))} -q tests", timeout=30, task_id="admitted-handoff", force=True))
        assert checked and not any(checked)
        assert result["exit_code"] == 0, result
        assert "completed" in result["output"]
        assert "session_id" not in result
    finally:
        os.close(fd)
        env.cleanup()


@pytest.mark.parametrize("via_code", [False, True])
@pytest.mark.parametrize("admit", [False, True])
def test_cron_waits_inline_until_admission_or_its_own_timeout(tmp_path, monkeypatch, via_code, admit):
    from gateway.session_context import preserved_session_context, set_turn_origin
    from tools.code_execution_tool import execute_code
    from tools.code_kernel import shutdown_all_kernels

    monkeypatch.delenv("HEAVY_SLOT_HELD", raising=False)
    monkeypatch.delenv("HERMES_HEAVY_SLOT", raising=False)
    monkeypatch.setenv("HERMES_TURN_ORIGIN", "user")  # task-local provenance wins
    ticket, started, release = (tmp_path / name for name in ("ticket", "started", "release"))
    os.mkfifo(release)
    fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    helper = _executable(tmp_path / "heavy-slot", f'''#!{sys.executable}
import os, sys
from pathlib import Path
assert not os.environ.get("HEAVY_SLOT_MAX_WAIT")
Path({str(ticket)!r}).touch()
with open({str(release)!r}, "rb", buffering=0) as stream:
    stream.read(1)
args = sys.argv[sys.argv.index("--") + 1:]
os.execvp(args[0], args)
''')
    runner = _executable(tmp_path / "pytest", f"#!/bin/sh\ntouch {shlex.quote(str(started))}\nprintf 'completed\\n'\n")
    monkeypatch.setattr(heavy, "_heavy_slot_executable", lambda _: str(helper))
    # Exercise both automatic promotion paths without a multi-minute test.
    monkeypatch.setattr(terminal_tool, "_FOREGROUND_HEAVY_SLOT_WAIT_CAP", 0)
    monkeypatch.setattr(terminal_tool, "FOREGROUND_MAX_TIMEOUT", 1)
    monkeypatch.setattr(terminal_tool, "spawn_background_process", lambda **_: pytest.fail("cron must stay inline"))
    monkeypatch.setattr(terminal_tool, "yield_to_background_handler", lambda **_: pytest.fail("cron must not adopt"))
    env = LocalEnvironment(cwd=str(tmp_path))
    wait_for_process = env._wait_for_process

    def wait_for_ticket(*args, **kwargs):
        assert kwargs.get("yield_after_seconds") is None
        assert "yield_handler" not in kwargs
        assert wait_until(ticket.exists, timeout=5)
        if admit:
            os.write(fd, b"x")
        return wait_for_process(*args, **kwargs)

    monkeypatch.setattr(env, "_wait_for_process", wait_for_ticket)
    monkeypatch.setattr(terminal_tool, "_acquire_env", lambda *_: env)
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {
        "env_type": "local", "cwd": str(tmp_path), "timeout": 2, "host_cwd": None,
    })
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda *_, **__: {"approved": True})
    monkeypatch.setattr("tools.code_execution_tool._load_config", lambda: {"mode": "strict", "timeout": 30})
    command = f"{shlex.quote(str(runner))} -q tests"
    try:
        with preserved_session_context():
            set_turn_origin("cron")
            if via_code:
                from model_tools import discover_builtin_tools
                discover_builtin_tools()
                cell = f"from hermes_tools import terminal\nimport json\nprint(json.dumps(terminal({command!r}, timeout=2)))"
                response = json.loads(execute_code(cell, task_id="cron-admission"))
                assert response["status"] == "success", response
                result = json.loads(response["output"])
            else:
                result = json.loads(terminal_tool.terminal_tool(command, timeout=2, task_id="cron-admission", force=True))
        assert "session_id" not in result, result
        assert "promoted_from_foreground" not in result, result
        if admit:
            assert result["exit_code"] == 0, result
            assert started.exists()
            assert "completed" in result["output"]
        else:
            assert result["exit_code"] == 124, result
            assert "not started" in result["output"]
            assert "waiting for heavy-slot admission" in result["output"]
            assert not started.exists()
    finally:
        shutdown_all_kernels()
        os.close(fd)
        env.cleanup()
