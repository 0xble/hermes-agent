"""Exit leaked test CLIs, gateways, and the tmux servers that held them.

A process whose ``HERMES_HOME`` is under a pytest tmp dir, or under a session
basetemp that wrote ``.hermes-pytest-owner``, is a test artifact. It exits when
that owner dies and when its age exceeds ``HERMES_TEST_MAX_LIFETIME`` (default
2 hours). A gateway whose home is the operator's install is never armed.

The session guardian is a child of pytest. SIGKILL of pytest does not kill it;
it notices the owner pid is gone and ``kill-server``s only the tmux servers
this session registered.
"""
from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_LIFETIME_S = 2 * 60 * 60
OWNER_MARKER = ".hermes-pytest-owner"
TMUX_REGISTRY = ".hermes-pytest-tmux"
_PATH_NEEDLES = ("pytest-of-", "/pytest-", "hermes-pytest")
_armed = False


def pytest_tmp_home(path: str) -> bool:
    """True when ``path`` is under a pytest tmp dir, not an operator home."""
    text = (path or "").replace("\\", "/").lower()
    return any(needle in text for needle in _PATH_NEEDLES)


def owner_pid_from_home(home: str) -> int | None:
    """The pytest pid recorded above ``home``, if that marker is still there."""
    if not home:
        return None
    try:
        current = Path(home).resolve()
    except OSError:
        return None
    for parent in (current, *current.parents):
        marker = parent / OWNER_MARKER
        if marker.is_file():
            try:
                return _positive_pid(marker.read_text(encoding="utf-8-sig").strip())
            except OSError:
                return None
    return None


def register_tmux_server(*, listener: str | None = None, socket: str | None = None) -> None:
    """Record a tmux server this pytest session started, so the guardian can kill it."""
    root = os.environ.get("HERMES_PYTEST_CUSTODY")
    if not root:
        return
    if listener:
        line = f"L {listener}\n"
    elif socket:
        line = f"S {socket}\n"
    else:
        return
    path = Path(root) / TMUX_REGISTRY
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def install_session_custody(basetemp: Path) -> None:
    """Mark this pytest session and start the guardian that outlives a killed pytest."""
    root = Path(basetemp)
    root.mkdir(parents=True, exist_ok=True)
    owner = os.getpid()
    (root / OWNER_MARKER).write_text(f"{owner}\n", encoding="utf-8")
    os.environ["HERMES_PYTEST_CUSTODY"] = str(root)
    os.environ["HERMES_TEST_OWNER_PID"] = str(owner)
    if os.name != "posix":
        return
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "guardian", str(owner), str(root)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def arm_test_artifact_lifetime() -> None:
    """Start the exit watch when this process is a pytest artifact. Never arms a real home."""
    global _armed
    if _armed:
        return
    home = os.environ.get("HERMES_HOME", "")
    # Path or the session marker decides. An inherited owner pid alone must not
    # arm a process whose home is the operator's install.
    if not pytest_tmp_home(home) and owner_pid_from_home(home) is None:
        return
    owner = _owner_pid(home)
    _armed = True
    if os.name == "posix":
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "artifact", str(os.getpid()),
             str(owner or 0), str(os.getppid()), str(_lifetime_s())],
            start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        # Windows has no POSIX parent/setsid contract. The independent wall-time
        # watchdog still enforces the deadline even when the CLI's GIL is stuck.
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "artifact", str(os.getpid()),
             str(owner or 0), "0", str(_lifetime_s())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def artifact_main(target: int, owner: int, parent: int, lifetime: float) -> None:
    """An independent process enforces the deadline even when the target is hung."""
    deadline = time.monotonic() + lifetime
    while _alive(target):
        # On POSIX this watcher stays our child until exit. Once reparented, the
        # target exited, so never send a signal to a potentially reused target pid.
        if os.name == "posix" and os.getppid() != target:
            return
        if ((owner and not _alive(owner)) or (parent > 1 and not _alive(parent))
                or time.monotonic() >= deadline):
            _kill_artifact(target)
            return
        time.sleep(0.25)


def guardian_main(owner: int, root: Path) -> None:
    deadline = time.monotonic() + _lifetime_s()
    while _alive(owner) and time.monotonic() < deadline:
        time.sleep(0.25)
    _kill_session_tmux(root)


def _owner_pid(home: str) -> int | None:
    marked = owner_pid_from_home(home)
    if marked is not None:
        return marked
    return _positive_pid(os.environ.get("HERMES_TEST_OWNER_PID", ""))


def _lifetime_s() -> float:
    raw = os.environ.get("HERMES_TEST_MAX_LIFETIME", "")
    if not raw:
        return float(DEFAULT_LIFETIME_S)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_LIFETIME_S)
    if not math.isfinite(value) or value <= 0:
        return float(DEFAULT_LIFETIME_S)
    return value


def _positive_pid(raw: str) -> int | None:
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if pid > 1 else None


def _alive(pid: int) -> bool:
    if pid <= 1:
        return False
    import psutil

    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _kill_artifact(target: int) -> None:
    if os.name == "posix":
        try:
            if os.getpgid(target) == target:
                os.killpg(target, signal.SIGKILL)  # windows-footgun: ok — guarded by os.name == posix
                return
        except ProcessLookupError:
            return
    try:
        os.kill(target, getattr(signal, "SIGKILL", signal.SIGTERM))
    except ProcessLookupError:
        pass


def _kill_session_tmux(root: Path) -> None:
    registry = root / TMUX_REGISTRY
    try:
        lines = registry.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    # Only registered tmux endpoints are ours. A gateway.sock below basetemp is
    # not a tmux server and can hold a tmux client open until its timeout.
    for line in lines:
        kind, _, value = line.partition(" ")
        if kind == "L" and value:
            _kill_listener(value)
        elif kind == "S" and value:
            _kill_socket(value)


def _kill_listener(name: str) -> None:
    _kill_tmux("-L", name)


def _kill_socket(path: str) -> None:
    _kill_tmux("-S", path)


def _kill_tmux(selector: str, value: str) -> None:
    try:
        subprocess.run(
            ["tmux", selector, value, "kill-server"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # A missing executable or one bad endpoint must not abort the sweep.
        pass


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "guardian":
        guardian_main(int(argv[2]), Path(argv[3]))
        return 0
    if len(argv) == 6 and argv[1] == "artifact":
        artifact_main(int(argv[2]), int(argv[3]), int(argv[4]), float(argv[5]))
        return 0
    arm_test_artifact_lifetime()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
