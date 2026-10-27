"""Pytest artifacts exit when their owner dies and after a hard lifetime."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from hermes_cli.test_artifact_lifetime import (
    OWNER_MARKER,
    guardian_main,
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


@pytest.mark.live_system_guard_bypass
def test_pytest_home_exits_when_the_lifetime_elapses():
    home = Path(tempfile.mkdtemp(prefix="pytest-of-lifetime-")) / "pytest-0" / "h"
    home.mkdir(parents=True)
    started = time.monotonic()
    result = _run_armed(home, timeout=4, HERMES_TEST_MAX_LIFETIME="0.4")
    assert result.returncode in {-signal.SIGKILL, 75}
    assert time.monotonic() - started < 3


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
        assert proc.returncode in {-signal.SIGKILL, 75}
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


@pytest.mark.live_system_guard_bypass
def test_guardian_kills_only_its_tmux_server():
    if subprocess.run(["tmux", "-V"], capture_output=True, check=False).returncode != 0:
        pytest.skip("tmux is not installed")
    # macOS rejects a tmux socket path longer than the sockaddr limit.
    short = Path(tempfile.mkdtemp(prefix="ht", dir="/tmp"))
    root = short / "root"
    root.mkdir()
    owned = root / "owned.sock"
    registered = short / "registered.sock"
    other = short / "other.sock"
    (root / ".hermes-pytest-tmux").write_text(f"S {registered}\n", encoding="utf-8")
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
            env={**os.environ, "PYTHONPATH": str(REPO)},
        )
        time.sleep(0.3)
        owner.kill()
        owner.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (_tmux_up(owned) or _tmux_up(registered)):
            time.sleep(0.1)
        assert not _tmux_up(owned)
        assert not _tmux_up(registered)
        assert _tmux_up(other)
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


def _tmux_up(sock: Path) -> bool:
    result = subprocess.run(
        ["tmux", "-S", str(sock), "ls"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0
