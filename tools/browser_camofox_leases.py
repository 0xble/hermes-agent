"""Host-wide exclusive tab leases for Camofox callers in separate Hermes processes.

OS locks are released on process death; no PID or age guessing authorizes deletion. The
coordinator is deliberately host-wide, not profile-scoped: external Camofox identities
can be shared across profiles. The directory contains only hashed resource keys.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading
from typing import Any

_lock = threading.RLock()
_handles: dict[tuple[str, str, str], Any] = {}


def _lease_root() -> Path:
    return Path.home() / ".local" / "state" / "hermes" / "camofox-tab-leases"


def _try_lock(handle: Any) -> bool:
    if os.name == "posix":
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    else:
        import portalocker
        try:
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.LockException:
            return False
    return True


def claim(server: str, user_id: str, tab_id: str) -> bool:
    """Claim a tab or confirm our existing lease; uncertainty refuses the claim."""
    key = (server, user_id, tab_id)
    with _lock:
        if key in _handles:
            return True
        handle = None
        try:
            root = _lease_root()
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest = hashlib.sha256(repr(key).encode()).hexdigest()
            # Never unlink lock files: replacing an inode permits two simultaneous locks.
            fd = os.open(root / digest, os.O_CREAT | os.O_RDWR, 0o600)
            handle = os.fdopen(fd, "r+b")
            if _try_lock(handle):
                _handles[key] = handle
                return True
        except (OSError, ImportError):
            return False
        finally:
            if handle is not None and key not in _handles:
                handle.close()
        return False


def release(server: str, user_id: str, tab_id: str) -> None:
    with _lock:
        handle = _handles.pop((server, user_id, tab_id), None)
        if handle is not None:
            handle.close()
