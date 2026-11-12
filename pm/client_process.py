"""A wall-clock bound for the isolated worker's request/response lifetime."""
from __future__ import annotations

from contextlib import contextmanager, suppress
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from pm.package import InstallError

# Minimum protocol lifetime. Command budgets are added by operation_budget.
WORKER_TIMEOUT = 3600


class WorkerDeadline:
    def __init__(self, timeout, result_received):
        self.timeout = timeout
        self.remaining = timeout
        self.result_received = result_received
        self.expired = threading.Event()
        self.timer = None

    def start(self, process):
        self.process = process
        self.resume()

    def _expire(self):
        if not self.result_received.is_set():
            self.expired.set()
            _kill_worker(self.process)

    def stop(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer.join()

    def suspend(self):
        self.stop()
        self.remaining -= time.monotonic() - self.started
        self.timer = None

    def resume(self):
        self.started = time.monotonic()
        self.timer = threading.Timer(max(0, self.remaining), self._expire)
        self.timer.daemon = True
        self.timer.start()

    def extend(self, seconds):
        running = self.timer is not None
        if running:
            self.suspend()
        self.remaining += seconds
        self.timeout += seconds
        if running:
            self.resume()


def _kill_tree(pid):
    # PM also runs before the application (or psutil) is installed.
    try:
        import psutil
    except ImportError:
        _kill_bootstrap_tree(pid)
        return
    try:
        root = psutil.Process(pid)
        descendants = root.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for child in [*reversed(descendants), root]:
        with suppress(psutil.NoSuchProcess):
            child.kill()


def _proc_parents(root):
    parents = []
    for stat in root.glob("[0-9]*/stat"):
        try:
            # comm is parenthesized and may itself contain spaces or ')'.
            fields = stat.read_text().rsplit(")", 1)[1].split()
            parents.append((int(stat.parent.name), int(fields[1])))
        except (OSError, ValueError, IndexError):
            continue  # Processes can disappear or become unreadable mid-snapshot.
    return parents


def _bootstrap_parents():
    try:
        snapshot = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                                  text=True, timeout=5, check=True)
        return [(int(pid), int(ppid)) for pid, ppid in
                (row.split() for row in snapshot.stdout.splitlines())]
    except (OSError, subprocess.SubprocessError, ValueError):
        proc = Path("/proc")
        return (_proc_parents(proc) or None) if proc.is_dir() else None


def _kill_bootstrap_tree(pid):
    """Early recovery has no site-packages, but must retain caller group custody."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, check=False)
        return
    parents = _bootstrap_parents()
    if parents is None:
        # Never signal the caller's shared group. A dedicated worker group is
        # safe to reap when neither process table is available.
        with suppress(ProcessLookupError):
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)  # windows-footgun: ok — POSIX branch, worker-owned group
                return
        parents = []
    tree = [pid]
    for parent in tree:
        tree.extend(child for child, owner in parents if owner == parent)
    for child in reversed(tree):
        with suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)  # windows-footgun: ok — POSIX branch, snapshot descendants only


def _kill_worker(process):
    if process.poll() is None:
        try:
            _kill_tree(process.pid)
        finally:
            process.kill()


@contextmanager
def worker_process(command, *, env, operation, timeout, result_received=None, deadline=None):
    result_received = result_received if result_received is not None else threading.Event()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               text=True, encoding="utf-8", env=env)

    deadline = deadline if deadline is not None else WorkerDeadline(timeout, result_received)
    deadline.start(process)
    try:
        yield process
    finally:
        deadline.stop()
        if process.poll() is None:
            _kill_worker(process)
        process.wait(timeout=5)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.close()
        process.stdout.close()
        if deadline.expired.is_set() and not result_received.is_set():
            raise InstallError("pm", f"worker {operation} timed out after {deadline.timeout}s",
                               "inspect the worker diagnostics on stderr") from None
