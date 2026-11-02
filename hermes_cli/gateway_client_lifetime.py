"""The finite client owns only gateways it actually spawned, never discovered owners."""
from __future__ import annotations

import subprocess
import threading


def live_client_child(child, lease, status) -> dict:
    """Only an invocation holding the live child handle may accept its finite endpoint."""
    if lease is not None and child is not None and status is None:
        return {"client_pid": child.pid}
    return {}


class ClientGatewayLease:
    def __init__(self):
        self.children: list[subprocess.Popen] = []
        self._closed = False
        self._lock = threading.Lock()

    def own(self, child: subprocess.Popen) -> None:
        with self._lock:
            if not self._closed:
                self.children.append(child)
                return
        # ensure may finish in an executor after client cancellation closed the lease.
        if child.stdin is not None:
            child.stdin.close()

    def close(self) -> None:
        """EOF requests shutdown even when readiness/attach failed; reap only our children."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            children, self.children = self.children, []
        for child in children:
            if child.stdin is not None:
                child.stdin.close()
        # Blocking reap trades about 3 s of normal exit latency (10 s worst case) for
        # returning only after the finite client's child has exited. Never detach this wait.
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Startup may never have reached the early watcher (e.g. interpreter loader).
                # A Popen handle, not discovery or a PID-file scan, is teardown authority.
                child.kill()
                child.wait(timeout=5)
