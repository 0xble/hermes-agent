"""Content-free timing for a compaction's publication on its worker thread.

Row changes include SQLite triggers/FTS and rolled-back attempts. These totals
complement the transcript writer's individual phase timings.
"""

import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar

_metrics: ContextVar[dict | None] = ContextVar("compaction_publication_metrics", default=None)


class LocalStateLockTimeout(sqlite3.OperationalError):
    """The local connection mutex, not another process's SQLite writer, timed out."""


@contextmanager
def publication_metrics(patience_s: float):
    metrics = {"publish_wait_ms": 0.0, "publish_hold_ms": 0.0, "publish_rows_written": 0,
               "publish_attempts": 0, "patience_s": patience_s}
    token = _metrics.set(metrics)
    try:
        yield metrics
    finally:
        _metrics.reset(token)


def record_publication_write(*, wait_s=0.0, hold_s=0.0, rows_written=0):
    metrics = _metrics.get()
    if metrics is not None:
        metrics["publish_wait_ms"] += wait_s * 1000
        metrics["publish_hold_ms"] += hold_s * 1000
        metrics["publish_rows_written"] += rows_written


@contextmanager
def state_lock(lock, *, deadline: float | None = None):
    """Bound writers, and publication's degraded reads, before entering a transaction.

    Never interrupt an acquired transaction. Ordinary readers retain their existing
    behavior. Publication reads on the degraded writer connection get the same
    patience as a writer, so display-plan preparation cannot bypass the bound.
    """
    metrics = _metrics.get()
    if deadline is None and metrics is None:
        with lock:
            yield
        return
    started = time.monotonic()
    if deadline is None:
        assert metrics is not None
        deadline = started + metrics["patience_s"]
    acquired = lock.acquire(timeout=max(0.0, deadline - started))
    record_publication_write(wait_s=time.monotonic() - started)
    if not acquired:
        raise LocalStateLockTimeout("database is locked (local SessionDB connection mutex patience exhausted)")
    try:
        yield
    finally:
        lock.release()


@contextmanager
def measure_write():
    """Separate transaction time from all admission/retry/maintenance waiting."""
    metrics = _metrics.get()
    if metrics is None:
        yield
        return
    started = time.monotonic()
    wait_before, hold_before = metrics["publish_wait_ms"], metrics["publish_hold_ms"]
    try:
        yield
    finally:
        outside_ms = (time.monotonic() - started) * 1000 - (metrics["publish_hold_ms"] - hold_before)
        metrics["publish_wait_ms"] = wait_before + max(0.0, outside_ms)


def record_publication_attempt():
    metrics = _metrics.get()
    if metrics is not None:
        metrics["publish_attempts"] += 1


def raise_if_nonretryable_contention(exc):
    """Expired patience and lost holders are terminal, not a live foreign writer."""
    from hermes_state_errors import CompressionLeaseLostError
    if isinstance(exc, (LocalStateLockTimeout, CompressionLeaseLostError)):
        raise exc
