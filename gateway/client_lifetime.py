"""Early, stdlib-only parent-death fence for a finite client's auto-started gateway.

stdin is a private pipe whose write end belongs only to the client. EOF works after
normal close, SIGTERM and SIGKILL, without PID reuse checks or Linux-only prctl.
Arm before the heavyweight gateway imports so a startup wedge is bounded too.
"""
from __future__ import annotations

import os
import signal
import stat
import sys
import threading
import time
from contextlib import suppress

# Deliberately independent of the configurable 600-second unmanaged idle exit.
CLIENT_EXIT_GRACE_SECONDS = 3.0
# Forced teardown is not a clean gateway exit; keep it distinct from restart/config codes.
CLIENT_LIFETIME_FORCED_EXIT_CODE = 74
# Launch-only secret: inherited across interpreter relaunches, never written to disk.
CLIENT_LEASE_TOKEN_ENV = "_HERMES_CLIENT_LEASE_TOKEN"
_client_lease_token = None


def arm_client_lifetime_for_argv(argv) -> None:
    """Fence only the private finite-client gateway launch, before heavy imports."""
    if "--client-owned" in argv and any(
            argv[i:i + 2] == ["gateway", "run"] for i in range(1, len(argv) - 1)):
        arm_client_lifetime()


def arm_client_lifetime() -> None:
    """On lease loss request ordinary shutdown, then force our OWN process out."""
    try:
        is_pipe = stat.S_ISFIFO(os.fstat(0).st_mode)
    except OSError:
        is_pipe = False
    if not is_pipe:
        raise SystemExit("--client-owned requires stdin to be a pipe owned by its finite client; "
                         "omit --client-owned to run a persistent gateway")
    global _client_lease_token
    _client_lease_token = os.environ.pop(CLIENT_LEASE_TOKEN_ENV, None)
    read_fd = os.dup(0)
    stream = os.fdopen(read_fd, "rb", buffering=0)
    thread = threading.Thread(target=_watch_client, args=(stream,), daemon=True,
                              name="gateway-client-lifetime")
    thread.start()


def client_lease_token() -> str | None:
    """Only private control identity may expose this in-memory creator credential."""
    return _client_lease_token


def _watch_client(stream) -> None:
    # No protocol payload is expected. Only the creating client owns the write end;
    # inherited tool/worker handles cannot accidentally extend the lease (close_fds).
    try:
        while stream.read(1):
            pass
    except OSError:
        pass  # a broken lifetime channel is lease loss, never permission to stay detached
    finally:
        stream.close()
    # Arm the hard bound first: gateway stop can wedge in an extension or busy loop.
    timer = threading.Timer(CLIENT_EXIT_GRACE_SECONDS, os._exit,
                            args=(CLIENT_LIFETIME_FORCED_EXIT_CODE,))
    timer.daemon = True
    timer.start()
    # Once installed, ordinary shutdown gets the full grace budget in parallel.
    shutdown_handler = os.name != "nt" and callable(signal.getsignal(signal.SIGTERM))
    if shutdown_handler:
        os.kill(os.getpid(), signal.SIGTERM)
    # Reuse only this process's already-loaded ownership registries, never a host
    # scan or a cold tool import. Detached terminal process groups otherwise outlive
    # the hard exit while normal daemon shutdown is still draining active work.
    foreground = sys.modules.get("tools.environments.base")
    kill_foreground = getattr(foreground, "kill_live_foreground_processes", None)
    if kill_foreground is not None:
        with suppress(Exception):
            kill_foreground(now=True)
    registry = getattr(sys.modules.get("tools.process_registry"), "process_registry", None)
    if registry is not None:
        with suppress(Exception):
            registry.kill_all(source="gateway_shutdown", deadline=time.monotonic() + 0.5)
    if not shutdown_handler:
        # During early startup SIGTERM's default action exits immediately; on
        # Windows os.kill(SIGTERM) uses TerminateProcess even with a handler.
        # Both would strand detached tools. Clean them first; the timer already
        # bounds cleanup and the registry gets only 0.5 s of its 3 s budget.
        os.kill(os.getpid(), signal.SIGTERM)
