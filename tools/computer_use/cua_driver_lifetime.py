"""Tie an embedded ``cua-driver serve`` process to the Hermes process that spawned it.

macOS launches the private daemon through ``/usr/bin/open`` so TCC stays on
``com.trycua.driver``. ``open`` exits immediately and LaunchServices does not
inherit a pipe, so the driver's ``--parent-liveness-stdio`` /
``CUA_DRIVER_PARENT_LIVENESS_STDIN`` watch cannot see this process. A detached
driver is born with ppid 1 and survives SIGKILL, a skipped atexit, and a
failed ``cua-driver stop``.

The owner is this process. It records its pid beside the ``hc-`` socket, holds
a pipe the watchdog reads, and on close kills the driver's process group.
The watchdog runs in its own session, so a process-group kill of Hermes still
leaves it alive long enough to reap when the pipe EOFs and the owner pid is
dead. A startup sweep reaps ppid-1 orphans of this user whose executable and
``hc-`` socket match, and removes stale sockets. It never signals a driver
whose recorded owner is still alive.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("tools.computer_use.cua_driver_lifetime")

_OWNER_GRACE_SECONDS = 5.0
_KILL_WAIT_SECONDS = 0.3
_SOCKET_PREFIX = "hc-"
_APP_BINARY = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"


@dataclass(frozen=True)
class EmbeddedProcess:
    pid: int
    ppid: int
    username: str
    exe: str
    cmdline: tuple[str, ...]
    create_time: float


class OwnerWatch:
    """Pipe write-end and watchdog child for one embedded driver."""

    def __init__(self, process: Any, write_fd: int | None, socket_path: str, exe: str) -> None:
        self.process, self.write_fd, self.socket_path, self.exe = process, write_fd, socket_path, exe

    def close(self) -> None:
        fd, self.write_fd = self.write_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            process.wait(timeout=1.0)
        except Exception:
            kill = getattr(process, "kill", None)
            if callable(kill):
                try:
                    kill()
                except Exception:
                    pass


def owner_record_path(socket_path: str) -> str:
    return f"{socket_path}.owner"


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _current_username() -> str:
    try:
        import psutil
        return psutil.Process().username()
    except Exception:
        return ""


def _process_create_time(pid: int) -> float | None:
    try:
        import psutil
        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def pid_alive(pid: int, create_time: float | None = None) -> bool:
    """True when ``pid`` is the same live process that recorded ``create_time``.

    A zombie still occupies the pid until its parent waits. That is not a live owner:
    the process is already dead, and treating it as alive would skip the reap.
    """
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        import psutil
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE or not proc.is_running():
            return False
        if create_time is not None and abs(float(proc.create_time()) - float(create_time)) > 0.05:
            return False
        return True
    except Exception:
        return False


def write_owner_record(socket_path: str, exe: str, *, pid: int | None = None) -> dict[str, Any]:
    owner = pid if pid is not None else os.getpid()
    record = {
        "pid": owner,
        "create_time": _process_create_time(owner),
        "exe": _realpath(exe) if exe else "",
        "socket": socket_path,
    }
    path = owner_record_path(socket_path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    os.replace(tmp, path)
    return record


def read_owner_record(socket_path: str) -> dict[str, Any] | None:
    try:
        with open(owner_record_path(socket_path), encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def owner_is_alive(socket_path: str) -> bool:
    record = read_owner_record(socket_path)
    if not record:
        return False
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    created = record.get("create_time")
    try:
        created_f = float(created) if created is not None else None
    except (TypeError, ValueError):
        created_f = None
    return pid_alive(pid, created_f)


def remove_owner_record(socket_path: str) -> None:
    with _suppress():
        os.remove(owner_record_path(socket_path))


def remove_socket(socket_path: str) -> None:
    """Unlink an ``hc-`` socket we own. Never unlink a regular file."""
    if not os.path.basename(socket_path).startswith(_SOCKET_PREFIX):
        return
    try:
        if not stat.S_ISSOCK(os.lstat(socket_path).st_mode):
            return
        os.remove(socket_path)
    except OSError:
        return


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, OSError)


def socket_from_argv(argv: tuple[str, ...] | list[str]) -> str | None:
    if "serve" not in argv or "--embedded" not in argv:
        return None
    try:
        index = argv.index("--socket")
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    path = argv[index + 1]
    if not os.path.basename(path).startswith(_SOCKET_PREFIX):
        return None
    return path


def exe_matches(proc: EmbeddedProcess, allow: set[str]) -> bool:
    raw = proc.exe or (proc.cmdline[0] if proc.cmdline else "")
    if not raw or not allow:
        return False
    return _realpath(raw) in allow


def _allow_paths(executables: set[str]) -> set[str]:
    # ``/tmp`` is a symlink on macOS. Compare real paths or a recorded argv misses.
    return {_realpath(path) for path in executables if path}


def driver_executables() -> set[str]:
    """Exact cua-driver binaries this user can have launched. Empty if none resolve."""
    found: set[str] = set()
    candidates = [_APP_BINARY, os.environ.get("HERMES_CUA_DRIVER_CMD", "").strip()]
    which = _which("cua-driver")
    if which:
        candidates.append(which)
    home_tools = os.path.expanduser("~/.hermes/tools")
    if os.path.isdir(home_tools):
        try:
            names = os.listdir(home_tools)
        except Exception:
            names = []
        for name in names:
            if name.startswith("cua-driver-"):
                candidates.append(os.path.join(
                    home_tools, name, "CuaDriver.app", "Contents", "MacOS", "cua-driver"))
    for raw in candidates:
        if not raw:
            continue
        path = _realpath(os.path.expanduser(raw))
        if os.path.isfile(path):
            found.add(path)
    return found


def _which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def default_temp_dirs() -> list[str]:
    dirs: list[str] = []
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True, timeout=2, stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        if out:
            dirs.append(out)
    dirs.append(__import__("tempfile").gettempdir())
    if os.path.isdir("/tmp"):
        dirs.append("/tmp")
    unique: list[str] = []
    seen: set[str] = set()
    for raw in dirs:
        path = _realpath(raw)
        if path in seen or not os.path.isdir(path):
            continue
        seen.add(path)
        unique.append(path)
    return unique


def scan_processes() -> list[EmbeddedProcess]:
    """List processes without stating every executable.

    Requesting ``exe`` for the whole table stats unrelated binaries, including
    ones under the real Hermes home, which the test I/O guard rejects. Only a
    cmdline that already looks like an embedded ``hc-`` serve is worth an exe
    lookup, and a failed lookup must not abort the scan.
    """
    import psutil
    found: list[EmbeddedProcess] = []
    for proc in psutil.process_iter(["pid", "ppid", "username", "cmdline", "create_time"]):
        try:
            info = proc.info
            cmdline = tuple(str(part) for part in (info.get("cmdline") or ()))
            exe = ""
            if socket_from_argv(cmdline):
                try:
                    exe = str(proc.exe() or "")
                except Exception:
                    exe = cmdline[0] if cmdline and os.path.isabs(cmdline[0]) else ""
            found.append(EmbeddedProcess(
                pid=int(info["pid"]),
                ppid=int(info.get("ppid") or 0),
                username=str(info.get("username") or ""),
                exe=exe,
                cmdline=cmdline,
                create_time=float(info.get("create_time") or 0.0),
            ))
        except Exception:
            continue
    return found


def kill_process_group(pid: int) -> None:
    """TERM then KILL the driver's own process group. Never signal pid 1 or our group."""
    if not isinstance(pid, int) or pid <= 1:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        return
    import signal
    sigterm = signal.SIGTERM
    sigkill = getattr(signal, "SIGKILL", sigterm)
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return
    if pgid <= 1 or pgid == os.getpgrp():
        _signal_pid(pid, sigterm)
        time.sleep(_KILL_WAIT_SECONDS)
        if pid_alive(pid):
            _signal_pid(pid, sigkill)
        return
    try:
        os.killpg(pgid, sigterm)  # windows-footgun: ok — POSIX-only branch (win32 returns above)
    except (ProcessLookupError, PermissionError, OSError):
        return
    time.sleep(_KILL_WAIT_SECONDS)
    if not pid_alive(pid):
        return
    try:
        os.killpg(pgid, sigkill)  # windows-footgun: ok — POSIX-only branch (win32 returns above)
    except (ProcessLookupError, PermissionError, OSError):
        return


def _signal_pid(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return


def find_driver(socket_path: str, allow: set[str], processes: list[EmbeddedProcess] | None = None) -> EmbeddedProcess | None:
    for proc in processes if processes is not None else scan_processes():
        if socket_from_argv(proc.cmdline) != socket_path:
            continue
        if exe_matches(proc, allow):
            return proc
    return None


def terminate_embedded_driver(
    socket_path: str,
    exe: str,
    *,
    executables: set[str] | None = None,
    processes: list[EmbeddedProcess] | None = None,
    kill=None,
) -> bool:
    """Kill the serve process for this socket. False when no matching process exists."""
    allow = _allow_paths(set(executables or ()))
    if exe:
        allow.add(_realpath(exe))
    if not allow:
        allow = driver_executables()
    proc = find_driver(socket_path, allow, processes)
    if proc is None:
        remove_socket(socket_path)
        remove_owner_record(socket_path)
        return False
    (kill or kill_process_group)(proc.pid)
    remove_socket(socket_path)
    remove_owner_record(socket_path)
    return True


def _live_parent_client(socket_path: str, processes: list[EmbeddedProcess], driver_pid: int) -> bool:
    for proc in processes:
        if proc.pid == driver_pid or proc.ppid == 1:
            continue
        if socket_path in proc.cmdline:
            return True
    return False


def sweep_orphaned_embedded_drivers(
    *,
    executables: set[str] | None = None,
    processes: list[EmbeddedProcess] | None = None,
    temp_dirs: list[str] | None = None,
    username: str | None = None,
    kill=None,
    now: float | None = None,
    grace_seconds: float = _OWNER_GRACE_SECONDS,
) -> list[int]:
    """Reap ppid-1 embedded drivers whose owner is dead, and remove stale ``hc-`` sockets.

    Returns the pids signalled. A live owner record, a direct child (ppid != 1),
    or another live-parent process that still names the socket is left alone.
    """
    allow = _allow_paths(set(executables) if executables is not None else driver_executables())
    if not allow:
        return []
    user = username if username is not None else _current_username()
    procs = list(processes) if processes is not None else scan_processes()
    moment = time.time() if now is None else now
    signal = kill or kill_process_group
    reaped: list[int] = []
    live_sockets: set[str] = set()
    for proc in procs:
        socket_path = socket_from_argv(proc.cmdline)
        if not socket_path or not exe_matches(proc, allow):
            continue
        if user and proc.username and proc.username != user:
            continue
        if proc.ppid != 1 or owner_is_alive(socket_path) or _live_parent_client(socket_path, procs, proc.pid):
            live_sockets.add(socket_path)
            continue
        if read_owner_record(socket_path) is None and moment - proc.create_time < grace_seconds:
            live_sockets.add(socket_path)
            continue
        signal(proc.pid)
        remove_socket(socket_path)
        remove_owner_record(socket_path)
        reaped.append(proc.pid)
        logger.info("reaped orphaned embedded cua-driver pid %s socket %s", proc.pid, socket_path)
    _remove_stale_sockets(temp_dirs if temp_dirs is not None else default_temp_dirs(), live_sockets)
    return reaped


def _remove_stale_sockets(temp_dirs: list[str], live_sockets: set[str]) -> None:
    for directory in temp_dirs:
        root = _realpath(directory)
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not name.startswith(_SOCKET_PREFIX):
                continue
            path = os.path.join(root, name)
            if os.path.dirname(_realpath(path)) != root:
                continue
            if name.endswith(".owner"):
                socket_path = path[: -len(".owner")]
                if socket_path in live_sockets or owner_is_alive(socket_path):
                    continue
                if not _socket_in_use(socket_path):
                    with _suppress():
                        os.remove(path)
                continue
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                continue
            if not stat.S_ISSOCK(mode):
                continue
            if path in live_sockets or owner_is_alive(path) or _socket_in_use(path):
                continue
            remove_socket(path)


def _socket_in_use(socket_path: str) -> bool:
    """True when a live process still names this socket. Best-effort, never raises."""
    try:
        for proc in scan_processes():
            if socket_path in proc.cmdline and pid_alive(proc.pid, proc.create_time):
                return True
    except Exception:
        return True
    return False


def arm_owner(socket_path: str, exe: str) -> OwnerWatch:
    """Record this process as the owner and start the pipe-EOF watchdog."""
    record = write_owner_record(socket_path, exe)
    read_fd, write_fd = os.pipe()
    cmd = [
        sys.executable, "-m", "tools.computer_use.cua_driver_lifetime", "--watch",
        socket_path, exe, str(record["pid"]), str(record.get("create_time") or ""),
    ]
    kwargs: dict[str, object] = {
        "stdin": read_fd,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(cmd, **kwargs)  # type: ignore[arg-type]
    except Exception:
        os.close(read_fd)
        os.close(write_fd)
        raise
    os.close(read_fd)
    return OwnerWatch(process, write_fd, socket_path, exe)


def watch_owner(socket_path: str, exe: str, owner_pid: int, owner_create_time: float | None) -> None:
    """Block until the owner's pipe EOFs, then reap only if that owner is dead."""
    try:
        while os.read(0, 65536):
            pass
    except OSError:
        pass
    if pid_alive(owner_pid, owner_create_time):
        return
    terminate_embedded_driver(socket_path, exe)


def hold_for_kill(socket_path: str, exe: str) -> None:
    """Test helper: arm a watchdog, announce readiness, and wait to be killed."""
    arm_owner(socket_path, exe)
    print("ready", flush=True)
    while True:
        time.sleep(3600)


def sweep_on_startup() -> None:
    """Gateway and CLI entry. Never raises and never kills a live owner's driver."""
    try:
        sweep_orphaned_embedded_drivers()
    except Exception:
        logger.debug("embedded cua-driver startup sweep failed", exc_info=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 3 and args[0] == "--hold":
        hold_for_kill(args[1], args[2])
        return 0
    if len(args) == 5 and args[0] == "--watch":
        created = float(args[4]) if args[4] not in {"", "None"} else None
        watch_owner(args[1], args[2], int(args[3]), created)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
