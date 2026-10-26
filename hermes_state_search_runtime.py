"""Reader-only admission, query budgets and timings for a complete recall operation.

Query-bearing recall has one cooperative SQLite deadline after admission.
Queryless browse/read/scroll retain their existing behavior and reader budget.
"""

from contextlib import contextmanager
from functools import wraps
from typing import Any
from dataclasses import dataclass, field
import logging
import sqlite3
import threading
import time
import weakref

logger = logging.getLogger("hermes_state")
_READER_WAIT_S = 5.0
_QUERY_BUDGET_S = 30.0
_MAX_WAITERS = 64
_local = threading.local()
_queues_lock = threading.Lock()
_queues = weakref.WeakKeyDictionary()


class _ReaderQueue:
    def __init__(self):
        self.condition = threading.Condition()
        self.waiters = []
        self.sequence = 0

    def checkout(self, db, priority):
        deadline = time.monotonic() + _READER_WAIT_S
        with self.condition:
            if len(self.waiters) >= _MAX_WAITERS:
                raise sqlite3.OperationalError("database busy: search reader queue full, retry")
            self.sequence += 1
            ticket = (priority, self.sequence)
            self.waiters.append(ticket)
        try:
            while True:
                with self.condition:
                    eligible = ticket == min(self.waiters)
                if eligible:
                    # Connection opens may themselves encounter SQLite contention.
                    # Never hold the queue mutex while opening: other waiters must
                    # still be able to expire or enqueue with higher priority.
                    conn = db._checkout_read_conn(independent=True)
                    if conn is not None:
                        with self.condition:
                            if ticket == min(self.waiters):
                                return conn
                        # A higher-priority caller arrived during the open. Give
                        # back its permit before rejoining admission, not the SQL.
                        db._return_read_conn(conn)
                        with self.condition:
                            self.condition.notify_all()
                with self.condition:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise sqlite3.OperationalError("database busy: search reader queue wait expired, retry")
                    # Ordinary readers also return permits, without notifying us.
                    self.condition.wait(min(remaining, 0.05))
        finally:
            with self.condition:
                self.waiters.remove(ticket)
                self.condition.notify_all()


def _queue_for(db):
    with _queues_lock:
        return _queues.setdefault(db._read_budget, _ReaderQueue())


def _priority():
    # Unknown/library callers use FIFO with interactive calls. Production cron
    # scopes bind origin/cron status, while inline children inherit parent source.
    from agent.delegation_context import is_delegated_child_context
    from gateway.session_context import (
        _CRON_SESSION, _SESSION_PLATFORM, _SESSION_SOURCE, _TURN_ORIGIN,
    )

    background = {"cron", "subagent", "batch", "kanban", "oneshot", "tool", "a2a"}
    # Inspect only task-bound signals. Ambient process state belongs to whichever
    # concurrent turn wrote last and must not promote or demote an unbound caller.
    return int(
        is_delegated_child_context()
        or _CRON_SESSION.get() == "1"
        or any(var.get() in background for var in (_SESSION_SOURCE, _SESSION_PLATFORM, _TURN_ORIGIN))
    )


@dataclass
class SearchOperation:
    db: Any
    caller: str
    started: float = field(default_factory=time.monotonic)
    connection: sqlite3.Connection | None = None
    reader_queue_ms: float = 0.0
    sql_ms: float = 0.0
    hydration_ms: float = 0.0
    path: str = "admission"
    rows: object = "err"
    filters: dict = field(default_factory=dict)
    sort: object = None
    query: str = ""
    stage_depth: dict = field(default_factory=dict)
    query_deadline: float | None = None
    query_budget_expired: bool = False


def current_search(db):
    operation = getattr(_local, "operation", None)
    return operation if operation is not None and operation.db is db else None


def set_search_path(db, path):
    operation = current_search(db)
    if operation is not None:
        operation.path = path


@contextmanager
def _query_budget(operation, query):
    # A queryless outer scope may start discovery later. Never renew its budget
    # for nested MATCH, LIKE fallbacks, metadata reads or hydration.
    if operation.query_deadline is None and isinstance(query, str) and query.strip():
        operation.query_deadline = time.monotonic() + _QUERY_BUDGET_S

        def progress():
            if time.monotonic() >= operation.query_deadline:
                operation.query_budget_expired = True
                return 1
            return 0

        operation.connection.set_progress_handler(progress, 1000)
    try:
        yield
    except sqlite3.OperationalError as exc:
        operation.rows = "err"
        if operation.query_budget_expired and "interrupt" in str(exc).lower():
            raise sqlite3.OperationalError(
                f"search too broad, refine the query or add source/time filters "
                f"({_QUERY_BUDGET_S:g}s query budget exceeded)"
            ) from exc
        raise


@contextmanager
def search_operation(db, *, caller="other", query="", sort=None, filters=None):
    existing = current_search(db)
    if existing is not None:
        existing.query, existing.sort = query, sort
        existing.filters.update(filters or {})
        with _query_budget(existing, query):
            yield existing
        return
    operation = SearchOperation(db, caller, query=query, sort=sort, filters=filters or {})
    previous = getattr(_local, "operation", None)
    admission = _queue_for(db)
    try:
        queued = time.monotonic()
        try:
            operation.connection = admission.checkout(db, _priority())
        finally:
            operation.reader_queue_ms = (time.monotonic() - queued) * 1000
        _local.operation = operation
        with _query_budget(operation, query):
            yield operation
    finally:
        _local.operation = previous
        if operation.connection is not None:
            if operation.query_deadline is not None:
                operation.connection.set_progress_handler(None, 0)
            db._return_read_conn(operation.connection)
            with admission.condition:
                admission.condition.notify_all()
        total_ms = (time.monotonic() - operation.started) * 1000
        from hermes_state_search import _search_slow_ms

        if total_ms >= _search_slow_ms():
            logger.info(
                "slow session search: caller=%s path=%s sort=%s filters=%s "
                "reader_queue_ms=%.3f sql_ms=%.3f hydration_ms=%.3f total_ms=%.3f "
                "rows=%s query=%r",
                caller, operation.path, operation.sort, operation.filters,
                operation.reader_queue_ms, operation.sql_ms, operation.hydration_ms,
                total_ms, operation.rows, operation.query[:200],
            )


@contextmanager
def browse_deadline(db, conn, timeout_seconds):
    """Keep ordinary browsing bounded without replacing a recall scope's budget."""
    if current_search(db) is not None:
        yield
        return
    deadline = time.monotonic() + timeout_seconds
    interrupted_by_deadline = False

    def progress():
        nonlocal interrupted_by_deadline
        if time.monotonic() >= deadline:
            interrupted_by_deadline = True
            return 1
        return 0

    conn.set_progress_handler(progress, 1000)
    try:
        yield
    except sqlite3.OperationalError as exc:
        if interrupted_by_deadline and "interrupt" in str(exc).lower():
            raise TimeoutError(f"recent-session browse exceeded {timeout_seconds:g}s deadline") from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)


def hydration_stage(fn):
    @wraps(fn)
    def timed(self, *args, **kwargs):
        with search_stage(self, "hydration_ms"):
            return fn(self, *args, **kwargs)
    return timed


@contextmanager
def search_stage(db, name):
    operation = current_search(db)
    if operation is None:
        yield
        return
    depth = operation.stage_depth.get(name, 0)
    operation.stage_depth[name] = depth + 1
    started, sql_before = time.monotonic(), operation.sql_ms
    try:
        yield
    finally:
        operation.stage_depth[name] = depth
        if depth == 0:
            elapsed = (time.monotonic() - started) * 1000
            if name == "hydration_ms":
                elapsed -= operation.sql_ms - sql_before
            setattr(operation, name, getattr(operation, name) + max(0.0, elapsed))
