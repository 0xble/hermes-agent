"""Early, stdlib-only parent-death fence for a finite client's auto-started gateway.

stdin is a private pipe whose write end belongs only to the client. EOF works after
normal close, SIGTERM and SIGKILL, without PID reuse checks or Linux-only prctl.
Arm before the heavyweight gateway imports so a startup wedge is bounded too.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from contextlib import suppress

# Deliberately independent of the configurable 600-second unmanaged idle exit.
CLIENT_EXIT_GRACE_SECONDS = 3.0


def arm_client_lifetime() -> None:
    """On lease loss request ordinary shutdown, then force our OWN process out."""
    stream = os.fdopen(os.dup(0), "rb", buffering=0)
    thread = threading.Thread(target=_watch_client, args=(stream,), daemon=True,
                              name="gateway-client-lifetime")
    thread.start()


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
    timer = threading.Timer(CLIENT_EXIT_GRACE_SECONDS, os._exit, args=(0,))
    timer.daemon = True
    timer.start()
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
    os.kill(os.getpid(), signal.SIGTERM)
