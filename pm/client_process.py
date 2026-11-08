"""A wall-clock bound for the isolated worker's request/response lifetime."""
from __future__ import annotations

from contextlib import contextmanager
import os
import subprocess
import threading

from pm.package import InstallError

# Downloads, unpacking and a dependency build can all occur in one request.
# Keep their existing per-step bounds, with a final ceiling for a silent worker.
WORKER_TIMEOUT = 3600


@contextmanager
def worker_process(command, *, env, operation, timeout):
    from hermes_cli._subprocess_compat import kill_process_tree

    expired = threading.Event()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               text=True, encoding="utf-8", env=env,
                               start_new_session=os.name != "nt")

    def expire():
        expired.set()
        kill_process_tree(process)

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        yield process
    finally:
        timer.cancel()
        timer.join()
        if process.poll() is None:
            kill_process_tree(process)
        process.wait(timeout=5)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.close()
        process.stdout.close()
        if expired.is_set():
            raise InstallError("pm", f"worker {operation} timed out after {timeout}s",
                               "inspect the worker diagnostics on stderr") from None
