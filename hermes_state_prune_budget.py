"""Cooperative retention budget, including SQL/FTS work inside a transaction.

SQLite progress handlers cannot interrupt an OS fsync or a blocked filesystem call.
Interrupted reclamation retains its durable retirement marker for the next pass.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
import math
import sqlite3
import time

_CURRENT = ContextVar("session_prune_budget", default=None)


class PruneBudgetExpired(RuntimeError):
    """The pass deadline or a single transaction's writer budget was exhausted."""


def validate_max_seconds(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                              or not math.isfinite(value) or value <= 0):
        raise ValueError("max_seconds must be a finite positive number")
    return value


class PruneBudget:
    TRANSACTION_SECONDS = 0.25

    def __init__(self, within_window=False, max_seconds=None):
        validate_max_seconds(max_seconds)
        self.deadline = math.inf if max_seconds is None else time.monotonic() + max_seconds
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
        if held_s >= budget.TRANSACTION_SECONDS:
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
    deadline = min(budget.deadline, time.monotonic() + budget.TRANSACTION_SECONDS) if write else budget.deadline
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
