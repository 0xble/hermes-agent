"""Snapshot preparation and optimistic publication for in-place session compaction."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import hashlib
import sqlite3
import time
from typing import Any

from hermes_state_transactions import execute_transcript_write
from hermes_state_write_metrics import current_write_metrics

# Below this width the original SQL content matches cost less than building an index
# and fencing a second read. Held-history/CAS proofs always use the snapshot.
UNRESOLVED_SNAPSHOT_THRESHOLD = 8


class CompactionSnapshotChanged(RuntimeError):
    """Retry preparation after releasing the writer transaction."""


def _session_freshness(conn: sqlite3.Connection, session_id: str) -> tuple:
    from agent.transcript_repair import _OWNED_COLUMNS

    session = conn.execute(
        'SELECT transcript_revision, runtime_revision, runtime_generation FROM sessions WHERE id = ?',
        (session_id,)).fetchone()
    # messages_transcript_revision_update fences the large transcript bodies, including writes
    # by old builds. Fence only the remaining CAS/identity columns here. Stream their digest:
    # sidecars can be large too, and no payload copy may ride across the commit.
    revision_columns = {'role', 'content', 'api_content', 'tool_calls', 'tool_call_id',
                        'tool_name', 'reasoning', 'reasoning_content', 'reasoning_details'}
    columns = ', '.join(dict.fromkeys(('id', 'timestamp', 'message_uid', 'tool_call_uid',
                                      *(c for c in _OWNED_COLUMNS if c not in revision_columns))))
    digest = hashlib.blake2b(digest_size=16)
    for row in conn.execute(
        f'SELECT {columns} FROM messages WHERE session_id = ? AND active = 1 ORDER BY id',
        (session_id,)):
        digest.update(repr(tuple(row)).encode('utf-8', 'surrogatepass'))
    return (tuple(session) if session is not None else None, digest.digest())


@dataclass
class PreparedCompaction:
    session_id: str
    freshness: tuple
    coverage: tuple[list[int], set[int]] | None
    elapsed_s: float
    error: Exception | None = None

    def validate(self, conn: sqlite3.Connection) -> None:
        if self.freshness != _session_freshness(conn, self.session_id):
            raise CompactionSnapshotChanged('session changed after compaction preparation')


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


@contextmanager
def _compaction_reader(db):
    conn = db._checkout_read_conn(independent=True)
    if conn is None:
        raise sqlite3.OperationalError('compaction independent reader unavailable')
    try:
        yield conn
    finally:
        db._return_read_conn(conn)


def snapshot_compaction(db, session_id, options):
    from agent.context_compressor import StaleHeldHistory
    from hermes_state_errors import HeldRowRewrittenError

    def read(conn):
        started = time.monotonic()
        error = None
        conn.execute('BEGIN')
        try:
            # The token and proof must describe the same SQLite snapshot.
            freshness = _session_freshness(conn, session_id)
            try:
                coverage = prepare_compaction(db, conn, session_id, options['covered_ids'],
                    options['unresolved_held'], options['watermark'], options['held_messages'], options['held_for_cas'])
            except (StaleHeldHistory, HeldRowRewrittenError) as exc:
                # Preserve lease error precedence, and revalidate before trusting a rejection.
                # A traceback would retain the proof's fetched payload rows across publication.
                coverage, error = None, exc.with_traceback(None)
        finally:
            conn.rollback()
        return PreparedCompaction(session_id, freshness, coverage, time.monotonic() - started, error)

    try:
        return db._read_retrying_ioerr(read, read_context=lambda: _compaction_reader(db))
    except sqlite3.Error:
        # Reader failures have already used the pool's retry/classification policy. The writer
        # transaction is the authoritative fallback and will still surface real storage failure.
        return None


def execute_prepared_compaction(db, session_id: str, messages: list[dict[str, Any]], **options):
    expensive = (options['held_messages'] is not None or options['held_for_cas'] is not None
                 or (options['covered_ids'] is not None
                     and len(options['unresolved_held'] or ()) > UNRESOLVED_SNAPSHOT_THRESHOLD))
    for attempt in range(3):
        fold_plan = db._display_fold_plan(session_id)
        prepared = snapshot_compaction(db, session_id, options) if expensive and attempt < 2 else None

        @wraps(db.archive_and_compact)
        def commit(conn):
            metrics = current_write_metrics.get()
            if metrics is not None and prepared is not None:
                metrics.add_time('compaction_prepare', prepared.elapsed_s)
            return db._archive_and_compact_on_conn(conn, session_id, messages,
                **options, fold_plan=fold_plan, prepared=prepared)

        try:
            return execute_transcript_write(db, commit, messages, session_id=session_id)
        except CompactionSnapshotChanged:
            if attempt == 2:
                raise
