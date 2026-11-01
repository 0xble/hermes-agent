"""Transaction settlement and bounded slow-write diagnostics for SessionDB."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from functools import wraps
from types import ModuleType
from typing import Any, Callable, TypeVar

from agent.message_metadata import CANONICAL_ROW, DB_ROW_SNAPSHOT
from hermes_state_write_metrics import WriteMetrics, current_write_metrics
from hermes_state_publication import record_publication_write
from hermes_state_write_stalls import current_observation


T = TypeVar("T")
_ROW_STATE_KEYS = ("_row_id", DB_ROW_SNAPSHOT, "timestamp")


def execute_transcript_write(
    db: Any, fn: Callable[[sqlite3.Connection], T], messages: list[dict[str, Any]], *,
    session_id: str, patience_s: float | None = None, session_ids: Any = (),
    skip_result: Any = None,
) -> T:
    """Restore caller row markers before each attempt and on final failure.

    A rolled-back insert's id can be reused by SQLite. Leaving its stamp on a caller's
    message would make a later flush adopt another writer's row and drop this message.
    Preserve the real callback name through the retry wrapper for lock attribution.
    """
    absent = object()
    pre_state = [tuple(msg.get(key, absent) for key in _ROW_STATE_KEYS) for msg in messages]

    def restore() -> None:
        for msg, state in zip(messages, pre_state):
            msg.pop(CANONICAL_ROW, None)
            for key, value in zip(_ROW_STATE_KEYS, state):
                if value is absent:
                    msg.pop(key, None)
                else:
                    msg[key] = value

    @wraps(fn)
    def attempt(conn):
        restore()
        return fn(conn)

    try:
        return db._execute_write(attempt, patience_s=patience_s,
            session_id=session_id, input_rows=len(messages),
            session_ids=session_ids, skip_result=skip_result)
    except BaseException:
        restore()
        raise


def settle_write_transaction(
    conn: sqlite3.Connection, fn: Callable[[sqlite3.Connection], T], *, clock: ModuleType,
    logger: logging.Logger, warn_s: float, acquire_s: float,
    session_id: str | None, input_rows: int | None,
) -> T:
    """Run the callback and settle an already acquired transaction, preserving its exception.

    Acquisition covers this attempt's process writer guard, generation check and BEGIN.
    Either acquisition or transaction hold time can trigger the warning. No payload or SQL is logged.
    """
    started = clock.monotonic()
    changes_before = conn.total_changes
    cpu_started = clock.thread_time()
    callback_s = callback_cpu_s = commit_s = commit_cpu_s = rollback_s = 0.0
    observation = current_observation.get()
    outcome = "rollback_failed"
    metrics = WriteMetrics()
    metrics_token = current_write_metrics.set(metrics)
    try:
        observation.phase("callback")
        try:
            result = fn(conn)
        finally:
            callback_s = clock.monotonic() - started
            callback_cpu_s = clock.thread_time() - cpu_started
            metrics.add_time("callback_total", callback_s)
            observation.end()
        observation.phase("commit")
        commit_started = clock.monotonic()
        commit_cpu_started = clock.thread_time()
        try:
            conn.commit()
        finally:
            commit_s = clock.monotonic() - commit_started
            commit_cpu_s = clock.thread_time() - commit_cpu_started
            observation.end()
            metrics.add_time("commit", commit_s)
        outcome = "committed"
        return result
    except BaseException:
        rollback_started = clock.monotonic()
        try:
            conn.rollback()
            outcome = "rolled_back"
        # health: allow BLE001 -- Preserve the original write error; rollback error text may contain payloads, so log only its bounded type.
        except Exception as rollback_error:
            logger.warning("state.db rollback failed: error_type=%s pid=%d",
                type(rollback_error).__name__[:80], os.getpid())
        finally:
            rollback_s = clock.monotonic() - rollback_started
            metrics.add_time("rollback", rollback_s)
        raise
    finally:
        current_write_metrics.reset(metrics_token)
        held_s = clock.monotonic() - started
        record_publication_write(hold_s=held_s, rows_written=conn.total_changes - changes_before)
        if held_s >= warn_s or acquire_s >= warn_s:
            name = getattr(fn, "__qualname__", type(fn).__qualname__)[:160]
            observation.warnings.append((logger,
                "state.db write transaction held the write lock for %.1fs: callback=%s pid=%d "
                "session=%r input_rows=%s acquire_s=%.3f callback_s=%.3f callback_cpu_s=%.3f "
                "commit_s=%.3f commit_cpu_s=%.3f native_tid=%d operation=%s rollback_s=%.3f outcome=%s %s",
                (held_s, name, os.getpid(), session_id[:128] if session_id is not None else None,
                 input_rows, acquire_s, callback_s, callback_cpu_s, commit_s, commit_cpu_s,
                 threading.get_native_id(), observation.operation, rollback_s, outcome, metrics.format())))
