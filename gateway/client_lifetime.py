"""Early, stdlib-only parent-death fence for a finite client's auto-started gateway.

stdin is a private pipe whose write end belongs only to the client. EOF works after
normal close, SIGTERM and SIGKILL, without PID reuse checks or Linux-only prctl.
Arm before the heavyweight gateway imports so a startup wedge is bounded too.
"""
from __future__ import annotations

import os
import signal
import threading

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
    os.kill(os.getpid(), signal.SIGTERM)
