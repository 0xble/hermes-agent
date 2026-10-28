"""Content-free timing for a compaction's publication on its worker thread.

Row changes include SQLite triggers/FTS and rolled-back attempts. These totals
complement the transcript writer's individual phase timings.
"""

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger(__name__)

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
def state_lock(lock, *, db_path, deadline: float | None = None):
    """Bound only compaction publication's admission, including degraded reads.

    Ordinary writers retain their unbounded mutex wait: their SQLite retry budget
    must not turn a queued transcript append into a failed write. Never interrupt
    an acquired transaction.
    """
    metrics = _metrics.get()
    if metrics is None:
        with lock:
            yield
        return
    started = time.monotonic()
    publication_deadline = started + metrics["patience_s"]
    deadline = publication_deadline if deadline is None else min(deadline, publication_deadline)
    patience_s = max(0.0, deadline - started)
    snapshot = getattr(lock, "owner_snapshot", None)
    acquisitions_before = snapshot()[0] if snapshot is not None else None
    acquired = lock.acquire(timeout=patience_s)
    record_publication_write(wait_s=time.monotonic() - started)
    if not acquired:
        from hermes_state_lockowners import log_write_lock_holders
        _log_mutex_owner(snapshot, acquisitions_before, patience_s)
        log_write_lock_holders(db_path, patience_s)
        raise LocalStateLockTimeout("database is locked (local SessionDB connection mutex patience exhausted)")
    try:
        yield
    finally:
        lock.release()


def _log_mutex_owner(snapshot, acquisitions_before, patience_s):
    count, owner = snapshot() if snapshot is not None else (None, None)
    acquisitions = count - acquisitions_before if count is not None else "unknown"
    if owner is None:
        detail = "owner=unavailable (uninstrumented mutex or acquire/release handoff)"
    else:
        held_s = max(0.0, time.monotonic() - owner.acquired_at)
        sites = " <- ".join(f"{os.path.basename(code.co_filename)}:{line} ({code.co_name})"
                            for code, line in owner.callsite)
        detail = (f"owner_thread={owner.thread_name!r} owner_ident={owner.thread_ident} "
                  f"held_for_s={held_s:.3f} owner_callsite={sites}")
    logger.warning("state.db local connection mutex unavailable for %.3fs: %s "
                   "acquisitions_during_wait=%s", patience_s, detail, acquisitions)


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
