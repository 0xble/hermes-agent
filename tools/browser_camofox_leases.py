"""Host-wide exclusive tab leases for Camofox callers in separate Hermes processes.

OS locks are released on process death; no PID or age guessing authorizes deletion. The
coordinator is deliberately host-wide, not profile-scoped: external Camofox identities
can be shared across profiles. The directory contains only hashed resource keys.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

_lock = threading.RLock()
_handles: dict[tuple[str, str, str], Any] = {}
_paths: dict[tuple[str, str, str], Path] = {}
_local_claims: set[tuple[str, str, str]] = set()
_fallback_warned = False
_pruned = False
logger = logging.getLogger(__name__)


def _lease_root() -> Path:
    # Gateways, CLIs and profiles can share one Camofox identity, so neither
    # process HERMES_HOME nor task-local profile scope may move the coordinator.
    return Path.home() / ".local" / "state" / "hermes" / "camofox-tab-leases"


def _lease_path(key: tuple[str, str, str]) -> Path:
    digest = hashlib.sha256(repr(key).encode()).hexdigest()
    return _lease_root() / digest


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


def _unlink_locked_path(handle: Any, path: Path) -> None:
    """Unlink only the authoritative inode while still holding its exclusive lock."""
    current = path.stat(follow_symlinks=False)
    opened = os.fstat(handle.fileno())
    if (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
        path.unlink()


def _prune_idle_files(root: Path) -> None:
    """Best-effort crash residue cleanup once on this process's first claim.

    Age/PIDs never grant cleanup authority: only a nonblocking exclusive lock does.
    Claimants validate their inode after locking, so preopened unlinked files are safe.
    """
    global _pruned
    if _pruned:
        return
    _pruned = True
    try:
        for path in root.iterdir():
            if len(path.name) != 64 or any(c not in "0123456789abcdef" for c in path.name):
                continue
            try:
                # Avoid briefly stealing a fresh tab's lease from a nonblocking claimant.
                # Age only defers pruning; the exclusive lock still authorizes removal.
                if time.time() - path.stat(follow_symlinks=False).st_mtime < 600:
                    continue
                fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "r+b") as handle:
                    if _try_lock(handle):
                        _unlink_locked_path(handle, path)
            except (OSError, ImportError):
                continue
    except OSError:
        pass


def _warn_process_local_fallback(exc: BaseException) -> None:
    global _fallback_warned
    if not _fallback_warned:
        logger.warning("Camofox tab leases unavailable; using process-local ownership: %s", exc)
        _fallback_warned = True


def claim(server: str, user_id: str, tab_id: str) -> bool:
    """Claim a tab or confirm our existing lease.

    A real non-blocking lock collision refuses the claim. State-directory failures and an
    unavailable optional Windows lock provider degrade to process-local ownership instead of
    making every newly-created tab unusable.
    """
    key = (server, user_id, tab_id)
    with _lock:
        if key in _handles or key in _local_claims:
            return True
        handle = None
        path: Path | None = None
        try:
            path = _lease_path(key)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _prune_idle_files(path.parent)
            while True:
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                handle = os.fdopen(fd, "r+b")
                if not _try_lock(handle):
                    return False
                # Release unlinks while locked. A claimant can have opened that old inode
                # just before unlink, then acquire it after close. Never publish that orphan
                # lease: only the inode still addressed by the resource path is authoritative.
                try:
                    current = path.stat()
                except FileNotFoundError:
                    handle.close()
                    handle = None
                    continue
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    handle.close()
                    handle = None
                    continue
                _handles[key] = handle
                _paths[key] = path
                return True
        except (OSError, ImportError) as exc:
            _local_claims.add(key)
            if handle is not None and path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
            _warn_process_local_fallback(exc)
            return True
        finally:
            if handle is not None and key not in _handles:
                handle.close()
        return False


def release(server: str, user_id: str, tab_id: str) -> None:
    key = (server, user_id, tab_id)
    with _lock:
        _local_claims.discard(key)
        handle = _handles.pop(key, None)
        path = _paths.pop(key, None)
        if handle is not None:
            # Remove the pathname while the exclusive lock is still held. This avoids
            # replacing an inode that another process may have locked between close/unlink.
            if path is not None:
                try:
                    _unlink_locked_path(handle, path)
                except OSError:
                    pass
            handle.close()
