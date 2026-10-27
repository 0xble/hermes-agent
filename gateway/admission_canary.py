"""Bounded, content-free native-refusal evidence for the release guardian."""
from __future__ import annotations

from asyncio import Task, current_task
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import os
import threading
import time
from collections import deque

# Retain longer than the maximum 5-minute observation window. The canary trips
# at 10 events by default, well below the 4096 retained observations.
_events: deque[tuple[float, str]] = deque(maxlen=4096)
_admitting: ContextVar[Task | None] = ContextVar("admission_canary_admitting", default=None)
_lock = threading.Lock()


def snapshot() -> dict:
    with _lock:
        cutoff = time.time() - 600
        while _events and _events[0][0] < cutoff:
            _events.popleft()
        return {"pid": os.getpid(), "refusals": list(_events)}


@contextmanager
def refusal_evidence(source: str) -> Iterator[None]:
    """Count a failed authority admission once, before any logging or notice dedup.

    The codec is also reused by automation, so counting at its individual raise
    sites either misses ledger/gate refusals or double-counts producer failures.
    """
    from hermes_state_runtime import RuntimeStoreError
    # Producer preparation can wrap the authority call. Only the outer scope
    # counts, but a newly scheduled task owns its own admission even when it
    # inherited this context from the task that accepted its predecessor.
    task = current_task()
    if _admitting.get() is task:
        yield
        return
    token = _admitting.set(task)
    try:
        yield
    except RuntimeStoreError as exc:
        # Intentional stale-work fences are not a failed admission of live work.
        # Unknown/restoring routes and missing live connectors still count, as do
        # all native refusals, even if they happen to use the same reason/field.
        stale = source == 'automation' and exc.reason == 'not_found' and exc.field in {
            'automation_target_gone', 'automation_target_suspended',
        }
        if not stale:
            clause = f"{source}: {exc.reason}" + (f" ({exc.field})" if exc.field else "")
            record_refusal(clause)
        raise
    finally:
        _admitting.reset(token)


def record_refusal(clause: str) -> None:
    """Record one refused admission attempt, not its logs or transport notices."""
    with _lock:
        _events.append((time.time(), str(clause)[:128]))
    # Use the existing coalescing background writer, never synchronous disk I/O
    # or user-facing notices on the admission path.
    from gateway.status import publish_runtime_status
    try:
        publish_runtime_status()
    except (OSError, RuntimeError):
        # A failed status write cannot replace the original admission refusal.
        # The next heartbeat publishes the retained evidence.
        import logging
        logging.getLogger(__name__).debug("Cannot publish admission evidence", exc_info=True)
