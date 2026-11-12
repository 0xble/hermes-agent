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
_UNIT_SECONDS = ContextVar("session_prune_unit_seconds", default=None)
logger = logging.getLogger(__name__)


class PruneBudgetExpired(RuntimeError):
    """The pass deadline or a single transaction's writer budget was exhausted."""


class PruneWriteContended(RuntimeError):
    """No writer lock was acquired. Yield before retrying the same work."""


def validate_max_seconds(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                              or not math.isfinite(value) or value <= 0):
        raise ValueError("max_seconds must be a finite positive number")
    return value


class PruneBudget:
    TRANSACTION_SECONDS = 0.25
    MIN_UNIT_CEILING_SECONDS = 2.0

    def __init__(self, within_window=False, max_seconds=None, *, db_path=None):
        validate_max_seconds(max_seconds)
        self.deadline = math.inf if max_seconds is None else time.monotonic() + max_seconds
        self.deferred_ids = set()
        self.db_path = db_path
        self.next_contention_warning = time.monotonic() + 30
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

    def warn_contention(self, label):
        now = time.monotonic()
        if math.isfinite(self.deadline) or now < self.next_contention_warning:
            return
        self.next_contention_warning = now + 30
        from hermes_state_lockowners import state_db_write_lock_holders
        holders = state_db_write_lock_holders(self.db_path) if self.db_path is not None else []
        logger.warning("Session prune %r continues retrying a foreign SQLite writer: %s",
                       label, " | ".join(holders) or "write-lock holder unavailable")


@contextmanager
def prune_budget(budget):
    token = _CURRENT.set(budget)
    try:
        budget.check()
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def without_prune_budget():
    token = _CURRENT.set(None)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_budget():
    return _CURRENT.get()


def run_prune_write(attempt, *, shrink=None, minimum_seconds=None, defer=None, label="maintenance", skip_result=None):
    """One recovery path for batches, pages and indivisible housekeeping writes.

    Callbacks run only after the previous transaction rolled back and released its
    lock. A shrink callback changes the next attempt's unit and returns True, or
    returns False at its indivisible minimum (including atomic local lineages).
    """
    budget = current_budget()
    if budget is None or _UNIT_SECONDS.get() is not None:
        return attempt()
    from hermes_state_prune_pacing import PrunePacer
    pacer = PrunePacer(max_size=1)
    seconds = budget.TRANSACTION_SECONDS
    while True:
        budget.check()
        # A byte estimate is useful only for a minimum page. Work cost learned
        # from a rollback supplies the ceiling attempt when bytes underestimate.
        if minimum_seconds is not None:
            seconds = max(seconds, min(budget.MIN_UNIT_CEILING_SECONDS, minimum_seconds()))
        unit_label = label() if callable(label) else label
        if seconds > budget.TRANSACTION_SECONDS:
            logger.warning("Session cleanup %r uses the %.2fs minimum-unit writer ceiling", unit_label, seconds)
        started = time.monotonic()
        token = _UNIT_SECONDS.set(seconds)
        try:
            return attempt()
        except PruneWriteContended:
            budget.warn_contention(unit_label)
            pacer.pause(0)
            continue
        except PruneBudgetExpired:
            budget.check()
        finally:
            _UNIT_SECONDS.reset(token)
        held_s = time.monotonic() - started
        pacer.pause(held_s)
        if shrink is not None and shrink():
            seconds = budget.TRANSACTION_SECONDS
        elif seconds == budget.TRANSACTION_SECONDS:
            seconds = min(budget.MIN_UNIT_CEILING_SECONDS, max(seconds * 2, held_s * 2))
        else:
            logger.error("Session cleanup %r could not complete within the %.2fs writer ceiling; cleanup deferred",
                         unit_label, seconds)
            return defer() if defer is not None else skip_result


def transaction_seconds(budget):
    return _UNIT_SECONDS.get() or budget.TRANSACTION_SECONDS


def write_patience(patience_s, deadline):
    budget = current_budget()
    if budget is not None:
        budget.check()
        deadline = budget.deadline
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
    # Do not queue a maintenance write behind a foreground writer. Reads retain
    # the normal busy wait, including in rollback-journal mode.
    if write:
        conn.execute("PRAGMA busy_timeout=0")
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        yield
    except sqlite3.OperationalError as exc:
        from hermes_state_errors import is_sqlite_lock_error
        if write and is_sqlite_lock_error(exc):
            raise PruneWriteContended("session prune yielded to a foreground SQLite writer") from exc
        if time.monotonic() >= deadline or "interrupted" in str(exc).lower():
            raise PruneBudgetExpired("session prune SQL interrupted at its budget") from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.execute(f"PRAGMA busy_timeout={int(previous)}")
