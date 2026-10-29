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
import threading
import traceback
from typing import Optional, Set, Tuple

logger = logging.getLogger(__name__)

_loop_thread: Optional[int] = None
_reported: Set[Tuple[str, int]] = set()
_reported_lock = threading.Lock()
# Frames from these files are the lock plumbing, not the caller that needs fixing.
_PLUMBING = ("hermes_loop_guard.py", "hermes_state", "session_persistence.py", "session_lifecycle.py",
             "gateway/session.py", "session_db_recovery.py", "threading.py", "contextlib.py")


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


class LoopGuardedLock:
    """``threading.Lock`` that reports a blocking acquisition on the gateway loop thread."""

    __slots__ = ("_inner", "_what")

    def __init__(self, what: str) -> None:
        self._inner = threading.Lock()
        self._what = what

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if blocking and _loop_thread is not None:
            check(self._what)
        return self._inner.acquire(blocking, timeout)

    def release(self) -> None:
        self._inner.release()

    def locked(self) -> bool:
        return self._inner.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self._inner.release()


def _reset_for_tests() -> None:
    set_loop_thread(None)
    with _reported_lock:
        _reported.clear()


__all__ = ["LoopGuardedLock", "LoopThreadBlockingError", "check", "set_loop_thread"]
