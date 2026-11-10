"""Cooperative retention budget, including SQL/FTS work inside a transaction.

SQLite progress handlers cannot interrupt an OS fsync or a blocked filesystem call.
Interrupted reclamation retains its durable retirement marker for the next pass.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
import math
import logging
import sqlite3
import time

_CURRENT = ContextVar("session_prune_budget", default=None)
_PAGE_SECONDS = ContextVar("session_prune_page_seconds", default=None)
logger = logging.getLogger(__name__)


class PruneBudgetExpired(RuntimeError):
    """The pass deadline or a single transaction's writer budget was exhausted."""


def validate_max_seconds(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                              or not math.isfinite(value) or value <= 0):
        raise ValueError("max_seconds must be a finite positive number")
    return value


class PruneBudget:
    TRANSACTION_SECONDS = 0.25
    MIN_UNIT_CEILING_SECONDS = 2.0

    def __init__(self, within_window=False, max_seconds=None):
        validate_max_seconds(max_seconds)
        self.deadline = math.inf if max_seconds is None else time.monotonic() + max_seconds
        self.deferred_ids = set()
        if within_window:
            import hermes_time
            from hermes_cli.maintenance_window import deletion_window_open, deletion_window_spec, window_contains
            now, spec = hermes_time.now(), deletion_window_spec()
            if not window_contains(spec, now):
                # Admission belongs to the same policy the caller checked. A closed
                # fallback spec must not cancel an admitted always-open policy.
                if not deletion_window_open():
                    self.deadline = time.monotonic()
            elif spec is not None and str(spec).strip():
                # Find the first closed minute, including windows crossing midnight.
                minute = now.replace(second=0, microsecond=0)
                for offset in range(1, 1441):
                    end = minute + timedelta(minutes=offset)
                    if not window_contains(spec, end):
                        self.deadline = min(self.deadline, time.monotonic() + end.timestamp() - now.timestamp())
                        break

    def check(self):
        if time.monotonic() >= self.deadline:
            raise PruneBudgetExpired("session prune stopped at its deadline; unfinished cleanup remains resumable")

    def pause(self, seconds):
        self.check()
        time.sleep(min(seconds, max(0, self.deadline - time.monotonic())))
        self.check()


@contextmanager
def prune_budget(budget):
    token = _CURRENT.set(budget)
    try:
        budget.check()
        yield
    finally:
        _CURRENT.reset(token)


def current_budget():
    return _CURRENT.get()


@contextmanager
def prune_page_budget(session_id, single_row_bytes, expired_attempts=0):
    """Scale only an indivisible row's writer hold, with the same foreground yield."""
    from hermes_state_deletion import DELETE_BYTES
    budget = current_budget()
    if budget is None:
        yield
        return
    seconds = min(budget.MIN_UNIT_CEILING_SECONDS,
                  budget.TRANSACTION_SECONDS * max(1, single_row_bytes / DELETE_BYTES))
    # Bytes are only an estimate of trigger cost. A minimum unit that already
    # expired must get a ceiling attempt even when its payload is small.
    if single_row_bytes and expired_attempts:
        seconds = budget.MIN_UNIT_CEILING_SECONDS
    ceiling = seconds == budget.MIN_UNIT_CEILING_SECONDS
    if ceiling:
        logger.warning("Session cleanup %r uses the %.2fs indivisible-row writer ceiling (%d bytes)",
                       session_id, seconds, single_row_bytes)
    token = _PAGE_SECONDS.set(seconds)
    try:
        yield
    except PruneBudgetExpired:
        budget.check()
        if ceiling:
            logger.error("Session cleanup %r could not complete within the %.2fs writer ceiling; cleanup deferred",
                         session_id, seconds)
        raise
    finally:
        _PAGE_SECONDS.reset(token)


def transaction_seconds(budget):
    return _PAGE_SECONDS.get() or budget.TRANSACTION_SECONDS


def write_patience(patience_s, deadline):
    budget = current_budget()
    if budget is not None:
        budget.check()
        patience_s = min(patience_s, 0.1, max(0, budget.deadline - time.monotonic()))
        deadline = min(deadline, time.monotonic() + patience_s)
    return patience_s, deadline


def check_prune_transaction(held_s):
    if budget := current_budget():
        budget.check()
        if held_s >= transaction_seconds(budget):
            raise PruneBudgetExpired("session prune transaction exceeded its writer budget")


def check_prune_budget():
    if budget := current_budget():
        budget.check()


@contextmanager
def prune_sql(conn, *, write=False):
    """Called only while the connection is exclusively borrowed by this operation."""
    budget = current_budget()
    if budget is None:
        yield
        return
    budget.check()
    deadline = min(budget.deadline, time.monotonic() + transaction_seconds(budget)) if write else budget.deadline
    previous = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    # Do not queue maintenance behind a foreground writer for the ordinary 1s busy timeout.
    conn.execute("PRAGMA busy_timeout=0")
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        yield
    except sqlite3.OperationalError as exc:
        from hermes_state_errors import is_sqlite_lock_error
        if is_sqlite_lock_error(exc):
            raise PruneBudgetExpired("session prune yielded to a foreground SQLite writer") from exc
        if time.monotonic() >= deadline or "interrupted" in str(exc).lower():
            raise PruneBudgetExpired("session prune SQL interrupted at its budget") from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.execute(f"PRAGMA busy_timeout={int(previous)}")
