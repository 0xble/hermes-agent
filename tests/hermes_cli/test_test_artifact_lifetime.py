"""Pytest artifacts exit when their owner dies and after a hard lifetime."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from hermes_cli.test_artifact_lifetime import (
    OWNER_MARKER,
    owner_pid_from_home,
    pytest_tmp_home,
    register_tmux_server,
)

REPO = Path(__file__).resolve().parents[2]


def test_pytest_tmp_home_matches_pytest_dirs_only():
    assert pytest_tmp_home("/private/var/folders/xx/pytest-of-brianle/pytest-0/h")
    assert pytest_tmp_home("/tmp/pytest-of-user/pytest-1/hermes_test")
    assert pytest_tmp_home("/var/folders/b/hermes-pytest-basetemp/case")
    assert not pytest_tmp_home("/Users/brianle/.hermes")
    assert not pytest_tmp_home("")
    assert not pytest_tmp_home("/private/var/folders/xx/T")


def test_owner_marker_is_found_above_the_home(tmp_path: Path):
    root = tmp_path / "session"
    home = root / "pytest-0" / "hermes_test"
    home.mkdir(parents=True)
    (root / OWNER_MARKER).write_text("4242\n", encoding="utf-8")
    assert owner_pid_from_home(str(home)) == 4242
    outside = Path(tempfile.mkdtemp(prefix="no-marker-"))
    assert owner_pid_from_home(str(outside)) is None


def test_register_is_a_no_op_without_a_session(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HERMES_PYTEST_CUSTODY", raising=False)
    register_tmux_server(listener="hermes-e2e-none")
    assert not (tmp_path / ".hermes-pytest-tmux").exists()


@pytest.mark.parametrize("started", [1.0, 2.0])
def test_watch_preserves_detached_artifacts_and_reused_pids(monkeypatch, started):
    import psutil
    from types import SimpleNamespace
    from hermes_cli import test_artifact_lifetime as lifetime

    running = iter([True, False])
    process = SimpleNamespace(create_time=lambda: 1.0,
                              is_running=lambda: next(running), status=lambda: psutil.STATUS_RUNNING)
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    monkeypatch.setattr(lifetime.os, "getppid", lambda: 123)
    monkeypatch.setattr(lifetime, "_alive", lambda pid: pid == 456)
    monkeypatch.setattr(lifetime.time, "sleep", lambda seconds: None)
    killed = []
    monkeypatch.setattr(lifetime, "_kill_artifact", killed.append)
    # The launcher (789) is dead, but the pytest owner (456) is alive. A reused
    # target pid (birth 1 != expected 2) must also be ignored before any signal.
    lifetime.artifact_main(123, 456, 789, 60, started)
    assert killed == []


@pytest.mark.parametrize("failure", [FileNotFoundError("tmux"), subprocess.TimeoutExpired("tmux", 5)])
def test_tmux_cleanup_continues_after_a_bad_endpoint(tmp_path, monkeypatch, failure):
    from hermes_cli import test_artifact_lifetime as lifetime

    (tmp_path / lifetime.TMUX_REGISTRY).write_text("L broken\nS registered\n", encoding="utf-8")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            raise failure
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(lifetime.subprocess, "run", run)
    lifetime._kill_session_tmux(tmp_path)
    assert calls == [["tmux", "-L", "broken", "kill-server"],
                     ["tmux", "-S", "registered", "kill-server"]]


@pytest.mark.platforms("posix")
def test_tmux_cleanup_does_not_probe_gateway_sockets(tmp_path, monkeypatch):
    import socket
    from hermes_cli import test_artifact_lifetime as lifetime

    with tempfile.TemporaryDirectory(prefix="ht") as short, socket.socket(socket.AF_UNIX) as gateway:
        root = Path(short)
        gateway.bind(str(root / "gateway.sock"))
        gateway.listen()
        (root / lifetime.TMUX_REGISTRY).write_text("L registered\n", encoding="utf-8")
        calls = []
        monkeypatch.setattr(lifetime.subprocess, "run", lambda argv, **kw: calls.append(argv))
        lifetime._kill_session_tmux(root)
        assert calls == [["tmux", "-L", "registered", "kill-server"]]


def _child_env(home: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("PYTEST") and k not in {"HERMES_TEST_OWNER_PID", "HERMES_PYTEST_CUSTODY"}
    }
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO)
    env.update(extra)
    return env


def _run_armed(home: Path, *, timeout: float, **extra: str) -> subprocess.CompletedProcess:
    script = (
        "from hermes_cli.test_artifact_lifetime import arm_test_artifact_lifetime\n"
        "arm_test_artifact_lifetime()\n"
        "import time\n"
        "time.sleep(8)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        env=_child_env(home, **extra),
        timeout=timeout,
        check=False,
    )


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_pytest_home_exits_when_the_lifetime_elapses():
    home = Path(tempfile.mkdtemp(prefix="pytest-of-lifetime-")) / "pytest-0" / "h"
    home.mkdir(parents=True)
    started = time.monotonic()
    result = _run_armed(home, timeout=4, HERMES_TEST_MAX_LIFETIME="0.4")
    assert result.returncode == -signal.SIGKILL  # windows-footgun: ok — POSIX-only test
    assert time.monotonic() - started < 3


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_pytest_home_exits_when_its_owner_dies():
    home = Path(tempfile.mkdtemp(prefix="pytest-of-owner-")) / "pytest-0" / "h"
    home.mkdir(parents=True)
    owner = subprocess.Popen(["sleep", "30"])
    proc = None
    try:
        script = (
            "from hermes_cli.test_artifact_lifetime import arm_test_artifact_lifetime\n"
            "arm_test_artifact_lifetime()\n"
            "import time\n"
            "time.sleep(8)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=REPO,
            env=_child_env(home, HERMES_TEST_OWNER_PID=str(owner.pid)),
        )
        time.sleep(0.3)
        owner.kill()
        owner.wait(timeout=5)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("artifact stayed up after its owner died")
        assert proc.returncode == -signal.SIGKILL  # windows-footgun: ok — POSIX-only test
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.live_system_guard_bypass
def test_operator_home_is_not_armed():
    home = Path(tempfile.mkdtemp(prefix="operator-home-"))
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "from hermes_cli.test_artifact_lifetime import arm_test_artifact_lifetime\n"
         "arm_test_artifact_lifetime()\n"
         "import time\n"
         "time.sleep(0.8)\n"],
        cwd=REPO,
        env=_child_env(home, HERMES_TEST_MAX_LIFETIME="0.2", HERMES_TEST_OWNER_PID="999999"),
    )
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("operator home was armed")
    assert proc.returncode == 0


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("expire", [False, True])
def test_guardian_kills_only_its_tmux_server(expire):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    # macOS rejects a tmux socket path longer than the sockaddr limit.
    short = Path(tempfile.mkdtemp(prefix="ht", dir="/tmp"))
    root = short / "root"
    root.mkdir()
    owned = root / "owned.sock"
    registered = short / "registered.sock"
    other = short / "other.sock"
    (root / ".hermes-pytest-tmux").write_text(f"S {owned}\nS {registered}\n", encoding="utf-8")
    owner = subprocess.Popen(["sleep", "30"])
    servers = []
    try:
        for sock in (owned, registered, other):
            servers.append(subprocess.Popen(
                ["tmux", "-S", str(sock), "-f", os.devnull, "new-session", "-d", "-s", "p", "sleep", "30"],
            ))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not all(path.exists() for path in (owned, registered, other)):
            time.sleep(0.05)
        assert owned.exists() and registered.exists() and other.exists()
        guardian = subprocess.Popen(
            [sys.executable, "-c",
             "import sys\n"
             "from pathlib import Path\n"
             "from hermes_cli.test_artifact_lifetime import guardian_main\n"
             "guardian_main(int(sys.argv[1]), Path(sys.argv[2]))\n",
             str(owner.pid), str(root)],
            cwd=REPO,
            env={**os.environ, "PYTHONPATH": str(REPO),
                 "HERMES_TEST_MAX_LIFETIME": "0.5" if expire else "30"},
        )
        time.sleep(0.3)
        if not expire:
            owner.kill()
            owner.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (_tmux_up(owned) or _tmux_up(registered)):
            time.sleep(0.1)
        assert not _tmux_up(owned)
        assert not _tmux_up(registered)
        assert _tmux_up(other)
        if expire:
            assert owner.poll() is None
            owner.kill()
            owner.wait(timeout=5)
        guardian.wait(timeout=5)
    finally:
        if owner.poll() is None:
            owner.kill()
        for sock in (owned, registered, other):
            subprocess.run(["tmux", "-S", str(sock), "kill-server"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for proc in servers:
            if proc.poll() is None:
                proc.kill()


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_lifetime_kills_a_stopped_cli():
    with tempfile.TemporaryDirectory(prefix="pytest-of-stopped-") as directory:
        home = Path(directory)
        ready = home / "ready"
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "from hermes_cli.test_artifact_lifetime import arm_test_artifact_lifetime\n"
             "arm_test_artifact_lifetime()\n"
             "import pathlib, sys, time\n"
             "pathlib.Path(sys.argv[1]).touch()\n"
             "time.sleep(30)\n", str(ready)],
            cwd=REPO, env=_child_env(home, HERMES_TEST_MAX_LIFETIME="1"),
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists()
            proc.send_signal(signal.SIGSTOP)
            assert proc.wait(timeout=4) == -signal.SIGKILL  # windows-footgun: ok — POSIX-only test
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_sigkill_of_real_pytest_reaps_its_artifacts(tmp_path: Path):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    import json
    import psutil
    import uuid

    listener = f"hermes-e2e-custody-{uuid.uuid4().hex[:8]}"
    evidence = tmp_path / "ready.json"
    nested = tmp_path / "test_owner.py"
    nested.write_text('''
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from hermes_cli.test_artifact_lifetime import install_session_custody, register_tmux_server

def test_owner(tmp_path):
    install_session_custody(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c",
         "from hermes_cli.test_artifact_lifetime import arm_test_artifact_lifetime; "
         "arm_test_artifact_lifetime(); import time; time.sleep(60)"],
        env={**os.environ, "HERMES_HOME": str(home), "HERMES_TEST_MAX_LIFETIME": "30"},
        start_new_session=True,
    )
    listener = os.environ["CUSTODY_TEST_LISTENER"]
    register_tmux_server(listener=listener)
    subprocess.run(["tmux", "-L", listener, "-f", os.devnull,
                    "new-session", "-d", "-s", "p", "sleep", "60"], check=True)
    Path(os.environ["CUSTODY_TEST_READY"]).write_text(json.dumps({"pid": child.pid}))
    time.sleep(60)
''', encoding="utf-8")
    env = _child_env(tmp_path, CUSTODY_TEST_LISTENER=listener, CUSTODY_TEST_READY=str(evidence))
    target = None
    with (tmp_path / "pytest.log").open("w", encoding="utf-8") as log:
        parent = subprocess.Popen([sys.executable, "-m", "pytest", "-q", str(nested)],
                                  cwd=REPO, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 45
            while not evidence.exists() and parent.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert evidence.exists(), (tmp_path / "pytest.log").read_text(encoding="utf-8-sig")
            target = psutil.Process(json.loads(evidence.read_text(encoding="utf-8-sig"))["pid"])
            parent.kill()
            assert parent.wait(timeout=5) == -signal.SIGKILL  # windows-footgun: ok — POSIX-only test
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                cli_up = target.is_running() and target.status() != psutil.STATUS_ZOMBIE
                tmux_up = subprocess.run(["tmux", "-L", listener, "ls"], capture_output=True, check=False).returncode == 0
                if not cli_up and not tmux_up:
                    break
                time.sleep(0.1)
            assert not target.is_running() or target.status() == psutil.STATUS_ZOMBIE
            assert subprocess.run(["tmux", "-L", listener, "ls"], capture_output=True, check=False).returncode != 0
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            if target is not None:
                try:
                    target.kill()
                except psutil.NoSuchProcess:
                    pass
            subprocess.run(["tmux", "-L", listener, "kill-server"], capture_output=True, check=False)


@pytest.mark.parametrize("raw", ["", "0", "-1", "nan", "inf", "invalid"])
def test_lifetime_cannot_be_disabled_by_an_invalid_value(monkeypatch, raw):
    from hermes_cli.test_artifact_lifetime import DEFAULT_LIFETIME_S, _lifetime_s

    monkeypatch.setenv("HERMES_TEST_MAX_LIFETIME", raw)
    assert _lifetime_s() == DEFAULT_LIFETIME_S


def _tmux_up(sock: Path) -> bool:
    result = subprocess.run(
        ["tmux", "-S", str(sock), "ls"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0
