"""A wall-clock bound for the isolated worker's request/response lifetime."""
from __future__ import annotations

from contextlib import contextmanager
import os
import signal
import subprocess
import threading

from pm.package import InstallError

# Downloads, unpacking and a dependency build can all occur in one request.
# Keep their existing per-step bounds, with a final ceiling for a silent worker.
WORKER_TIMEOUT = 3600


def _kill_worker(process):
    # PM also runs before the application (or hermes_cli helpers) is installed.
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, check=False)
        finally:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX branch, own session only
        except ProcessLookupError:
            return


@contextmanager
def worker_process(command, *, env, operation, timeout):
    expired = threading.Event()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               text=True, encoding="utf-8", env=env,
                               start_new_session=os.name != "nt")

    def expire():
        expired.set()
        _kill_worker(process)

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        yield process
    finally:
        timer.cancel()
        timer.join()
        if process.poll() is None:
            _kill_worker(process)
        process.wait(timeout=5)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.close()
        process.stdout.close()
        if expired.is_set():
            raise InstallError("pm", f"worker {operation} timed out after {timeout}s",
                               "inspect the worker diagnostics on stderr") from None
