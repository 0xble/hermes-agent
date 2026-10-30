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
_RETAIN_SECONDS = 600
# (at, clause, producer identity or None). A producer that requeues one refused
# input on backoff (a watch notice whose route check keeps failing) retries the
# same admission, not a new one: counting every retry let a single stuck notice
# from before a promotion trip its canary.
_events: deque[tuple[float, str, str | None]] = deque(maxlen=4096)
_admitting: ContextVar[Task | None] = ContextVar("admission_canary_admitting", default=None)
_lock = threading.Lock()


def snapshot() -> dict:
    with _lock:
        cutoff = time.time() - _RETAIN_SECONDS
        while _events and _events[0][0] < cutoff:
            _events.popleft()
        return {"pid": os.getpid(), "refusals": [(at, clause) for at, clause, _ in _events]}


@contextmanager
def refusal_evidence(source: str, identity: str | None = None) -> Iterator[None]:
    """Count a failed authority admission once, before any logging or notice dedup.

    ``identity`` is the producer's stable admission identity. Its retries count
    once per retention window; inputs without one count on every attempt.

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
        # Settled stale-work or absent legacy-delegation fences cannot recover
        # without human input. They are not evidence of a live-route outage.
        # Unknown routes, missing live connectors and every native refusal count.
        settled = source == 'automation' and exc.reason == 'not_found' and exc.field in {
            'automation_target_gone', 'automation_target_suspended',
            'automation_legacy_delegation',
        }
        if not settled:
            clause = f"{source}: {exc.reason}" + (f" ({exc.field})" if exc.field else "")
            record_refusal(clause, identity=f"{source}:{identity}" if identity else None)
        raise
    finally:
        _admitting.reset(token)


def record_refusal(clause: str, *, identity: str | None = None) -> None:
    """Record one refused admission, not its logs, transport notices or retries."""
    with _lock:
        now = time.time()
        cutoff = now - _RETAIN_SECONDS
        if identity is not None and any(
                counted == identity and at >= cutoff for at, _, counted in _events):
            return
        _events.append((now, str(clause)[:128], identity))
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
