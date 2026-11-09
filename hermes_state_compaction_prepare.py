"""Snapshot preparation and optimistic publication for in-place session compaction."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import sqlite3
import time
from typing import Any

from hermes_state_display_fold import _Freshness, _freshness
from hermes_state_publication import state_lock
from hermes_state_transactions import execute_transcript_write
from hermes_state_write_metrics import current_write_metrics


class CompactionSnapshotChanged(RuntimeError):
    """Retry preparation after releasing the writer transaction."""


@dataclass
class PreparedCompaction:
    freshness: _Freshness
    coverage: tuple[list[int], set[int]] | None
    elapsed_s: float
    error: Exception | None = None

    def validate(self, conn: sqlite3.Connection) -> None:
        if self.freshness != _freshness(conn):
            raise CompactionSnapshotChanged('database changed after compaction preparation')


def prepare_compaction(db, conn, session_id, covered_ids, unresolved_held, watermark,
                       held_messages, held_for_cas):
    if held_messages is not None:
        db._raise_if_unheld_summary(conn, session_id, held_messages)
    if held_for_cas:
        mismatches = getattr(db, "held_row_mismatches", None)
        if callable(mismatches) and mismatches(conn, session_id, held_for_cas):
            from hermes_state_errors import HeldRowRewrittenError
            raise HeldRowRewrittenError(
                f"a held row of session {session_id} was rewritten in place before the commit")
    return db._proved_coverage(conn, session_id, covered_ids, unresolved_held, watermark)


def snapshot_compaction(db, session_id, options):
    # Capture BEFORE the reader snapshot, including both same-handle and foreign commits.
    # Count/max/revision alone miss timestamp, summary-marker and other CAS-owned edits.
    with state_lock(db._lock, db_path=db.db_path):
        if db._conn is None:
            db._reopen_after_close_locked(context='compaction preparation')
        freshness = _freshness(db._conn)
    if freshness is None:
        raise RuntimeError('compaction preparation could not capture database freshness')
    conn = db._checkout_read_conn(independent=True)
    if conn is None:
        raise RuntimeError('compaction preparation requires an independent database reader')
    started = time.monotonic()
    error = None
    from agent.context_compressor import StaleHeldHistory
    from hermes_state_errors import HeldRowRewrittenError
    try:
        conn.execute('BEGIN')
        try:
            try:
                coverage = prepare_compaction(db, conn, session_id, options['covered_ids'],
                    options['unresolved_held'], options['watermark'], options['held_messages'], options['held_for_cas'])
            except (StaleHeldHistory, HeldRowRewrittenError) as exc:
                # Preserve lease error precedence, and revalidate before trusting a rejection.
                coverage, error = None, exc
        finally:
            conn.rollback()
    finally:
        db._return_read_conn(conn)
    return PreparedCompaction(freshness, coverage, time.monotonic() - started, error)


def execute_prepared_compaction(db, session_id: str, messages: list[dict[str, Any]], **options):
    for _attempt in range(3):
        fold_plan = db._display_fold_plan(session_id)
        prepared = snapshot_compaction(db, session_id, options)

        @wraps(db.archive_and_compact)
        def commit(conn):
            # Report the existing preparation phase, although it no longer contributes to hold time.
            metrics = current_write_metrics.get()
            if metrics is not None:
                metrics.add_time('compaction_prepare', prepared.elapsed_s)
            return db._archive_and_compact_on_conn(conn, session_id, messages,
                **options, fold_plan=fold_plan, prepared=prepared)

        try:
            return execute_transcript_write(db, commit, messages, session_id=session_id)
        except CompactionSnapshotChanged:
            continue
    raise CompactionSnapshotChanged('database changed during 3 compaction preparation attempts; refusing stale compaction')
