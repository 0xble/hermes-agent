"""Best-effort owner diagnostics stored inside the held cron jobs lock file."""

import json
import os
import sys
import time


def write_owner(lock_fd, operation):
    """Call only after a successful cross-process acquire, never in degraded mode."""
    owner = {
        "pid": os.getpid(),
        # Entry point, not arbitrary CLI arguments which may contain credentials or prompts.
        "command": f"{sys.executable} {sys.argv[0]}",
        "operation": operation,
        "started": time.monotonic(),
    }
    lock_fd.seek(0)
    lock_fd.truncate()
    json.dump(owner, lock_fd)
    lock_fd.flush()
    return owner


def describe_owner(lock_fd):
    """A timeout observes the current owner, not the waiter, without taking another lock."""
    try:
        lock_fd.seek(0)
        owner = json.loads(lock_fd.read(4096))
        held = max(0.0, time.monotonic() - float(owner["started"]))
        return (f"holder pid={int(owner['pid'])} command={owner['command']!r} "
                f"operation={owner['operation']!r} held={held:.3f}s")
    except (OSError, ValueError, TypeError, KeyError):
        return "holder metadata unavailable (older holder or release in progress)"


def clear_owner(lock_fd):
    """Clear before unlock so a subsequent timeout cannot attribute a departed owner."""
    lock_fd.seek(0)
    lock_fd.truncate()
    lock_fd.flush()
