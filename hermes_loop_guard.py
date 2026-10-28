"""Report state.db waits on the gateway's event-loop thread.

A turn worker can hold the SessionDB writer lock, or the session-store lock it keeps across a
state.db write, for seconds while ``BEGIN IMMEDIATE`` queues behind other writers. A loop thread
that waits on either misses liveness probes and the watchdog kills the gateway (exit 75); after a
restart the resumed-turn herd recreates the contention, so one such call turns a restart into a
crash loop. Loop code must reach state.db through ``asyncio.to_thread`` or an executor.

The gateway registers its loop thread while the liveness watchdog is armed. Locks that guard
state.db are :class:`LoopGuardedLock`; acquiring one (or opening a SessionDB) on that thread is
reported: raised under pytest so a new call site fails its test, logged once per call site in
production. Production never raises: the report must not become the crash it describes. Off the
registered thread the check is one integer compare.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from types import CodeType
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

_loop_thread: Optional[int] = None
_reported: set[tuple[str, int]] = set()
_reported_lock = threading.Lock()
# Frames from these files are the lock plumbing, not the caller that needs fixing.
_PLUMBING = ("hermes_loop_guard.py", "hermes_state", "session_persistence.py", "session_lifecycle.py",
             "gateway/session.py", "session_db_recovery.py", "threading.py", "contextlib.py")
# Owner attribution skips only the acquisition wrappers themselves, so SessionDB operations
# (hermes_state.py frames) stay visible as the holder.
_OWNER_WRAPPERS = ("hermes_loop_guard.py", "hermes_state_publication.py", "contextlib.py", "threading.py")
_OWNER_SITES = 3
_OWNER_MAX_WALK = 12


class LoopThreadBlockingError(RuntimeError):
    """A state.db lock or open was attempted on the gateway event-loop thread (tests only)."""


def set_loop_thread(ident: Optional[int]) -> None:
    """Register the gateway event-loop thread (``None`` disarms the guard)."""
    global _loop_thread
    _loop_thread = ident


def check(what: str) -> None:
    """Report *what* when it is about to block the registered loop thread."""
    if _loop_thread is None or threading.get_ident() != _loop_thread:
        return
    stack = traceback.extract_stack()[:-1]
    site = next((f for f in reversed(stack) if not any(p in f.filename.replace("\\", "/") for p in _PLUMBING)),
                stack[-1])
    message = f"{what} on the gateway event-loop thread from {site.filename}:{site.lineno} ({site.name})"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise LoopThreadBlockingError(message + "; move the call off the loop (asyncio.to_thread)")
    key = (site.filename, site.lineno)
    with _reported_lock:
        if key in _reported:
            return
        _reported.add(key)
    logger.warning("%s; it can stall the loop past the liveness watchdog:\n%s",
                   message, "".join(traceback.format_list(stack[-8:])).rstrip())


class LockOwner(NamedTuple):
    thread_name: str
    thread_ident: int
    acquired_at: float
    callsite: tuple[tuple[CodeType, int], ...]


class LoopGuardedLock:
    """``threading.Lock`` that reports a blocking acquisition on the gateway loop thread."""

    __slots__ = ("_inner", "_owner_state", "_what")

    def __init__(self, what: str) -> None:
        self._inner = threading.Lock()
        self._what = what
        self._owner_state: tuple[int, LockOwner | None] = (0, None)

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if blocking and _loop_thread is not None:
            check(self._what)
        acquired = self._inner.acquire(blocking, timeout)
        if acquired:
            acquired_at = time.monotonic()
            frame = sys._getframe(1)
            sites = []
            # Keep code/line pairs, never frames or their locals. Format only on timeout.
            # Skip the acquisition wrappers (``with lock``, state_lock, contextlib) so the
            # recorded sites name the operation that holds the lock.
            for _ in range(_OWNER_MAX_WALK):
                if frame is None or len(sites) == _OWNER_SITES:
                    break
                code = frame.f_code
                if not code.co_filename.endswith(_OWNER_WRAPPERS):
                    sites.append((code, frame.f_lineno))
                frame = frame.f_back
            del frame
            owner = LockOwner(threading.current_thread().name, threading.get_ident(),
                              acquired_at, tuple(sites))
            self._owner_state = (self._owner_state[0] + 1, owner)
        return acquired

    def owner_snapshot(self) -> tuple[int, LockOwner | None]:
        """Best-effort acquisition count and current owner, without another lock.

        Metadata can be absent during acquire/release handoff. The immutable pair
        keeps the sampled owner's identity and acquisition count together.
        """
        return self._owner_state

    def release(self) -> None:
        # Clear before unlocking so a successor's attribution cannot be erased.
        self._owner_state = (self._owner_state[0], None)
        self._inner.release()

    def locked(self) -> bool:
        return self._inner.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


def _reset_for_tests() -> None:
    set_loop_thread(None)
    with _reported_lock:
        _reported.clear()


__all__ = ["LoopGuardedLock", "LoopThreadBlockingError", "check", "set_loop_thread"]
