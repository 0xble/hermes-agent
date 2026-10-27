"""Exit leaked test CLIs, gateways, and the tmux servers that held them.

A process whose ``HERMES_HOME`` is under a pytest tmp dir, or under a session
basetemp that wrote ``.hermes-pytest-owner``, is a test artifact. It exits when
that owner dies and when its age exceeds ``HERMES_TEST_MAX_LIFETIME`` (default
2 hours). A gateway whose home is the operator's install is never armed.

The session guardian is a child of pytest. SIGKILL of pytest does not kill it;
it notices the owner pid is gone and ``kill-server``s only the tmux servers
this session registered or created under its basetemp.
"""
from __future__ import annotations

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
            return _positive_pid(marker.read_text(encoding="utf-8").strip())
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
    started = time.monotonic()
    lifetime = _lifetime_s()
    parent = os.getppid()
    watch_parent = parent > 1

    def watch() -> None:
        while True:
            if owner is not None and not _alive(owner):
                _die()
            if watch_parent and not _alive(parent):
                _die()
            if time.monotonic() - started >= lifetime:
                _die()
            time.sleep(0.25)

    import threading
    threading.Thread(target=watch, name="hermes-test-lifetime", daemon=True).start()


def guardian_main(owner: int, root: Path) -> None:
    while _alive(owner):
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
    if value <= 0:
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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _die() -> None:
    # SIGKILL the group when we lead it, then exit. ``_exit`` covers the race
    # where the signal has not been delivered before the next instruction.
    if os.name == "posix" and os.getpgrp() == os.getpid():
        try:
            os.killpg(os.getpid(), signal.SIGKILL)
        except OSError:
            pass
    try:
        os.kill(os.getpid(), signal.SIGKILL)
    except OSError:
        pass
    os._exit(75)


def _kill_session_tmux(root: Path) -> None:
    registry = root / TMUX_REGISTRY
    if registry.is_file():
        for line in registry.read_text(encoding="utf-8").splitlines():
            kind, _, value = line.partition(" ")
            if kind == "L" and value:
                _kill_listener(value)
            elif kind == "S" and value:
                _kill_socket(value)
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if path.is_socket():
                    _kill_socket(str(path))
            except OSError:
                continue


def _kill_listener(name: str) -> None:
    subprocess.run(
        ["tmux", "-L", name, "kill-server"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=5, check=False,
    )


def _kill_socket(path: str) -> None:
    subprocess.run(
        ["tmux", "-S", path, "kill-server"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=5, check=False,
    )


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "guardian":
        guardian_main(int(argv[2]), Path(argv[3]))
        return 0
    arm_test_artifact_lifetime()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
