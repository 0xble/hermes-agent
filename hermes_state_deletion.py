"""Resumable transcript reclamation behind the existing durable retirement fence.

The retirement transaction is the visibility boundary. Message deletion keeps all
FTS delete triggers enabled and yields the writer after a bounded page. No schema
migration, second transcript copy, or independent deletion identity is needed.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path

from hermes_state_common import _id_chunks, _placeholders
from hermes_state_keys import prefix_range

logger = logging.getLogger(__name__)

# A page bounds trigger work by rows AND serialized input. An oversized message
# must make progress alone. Window-bound retention interrupts SQLite VM work
# (including one trigger); SQLite cannot preempt an OS fsync.
DELETE_ROWS = 16
DELETE_BYTES = 64 * 1024
DELETE_SECONDS = 0.1


def visible_session_sql(session_id):
    from hermes_state_mutation_retirement import RETIRED_PREFIX
    return ("NOT EXISTS (SELECT 1 FROM state_meta AS retirement "
            f"WHERE retirement.key = '{RETIRED_PREFIX}' || {session_id})")


def stage_delete(conn, session_ids, sessions_dir=None):
    """Called after retirement, in the SAME transaction as guard/snapshot checks.

    Extend the retirement marker with unfinished physical work. The marker stays
    until cleanup is complete, including across the sweep retry horizon.
    """
    from hermes_state_mutation_retirement import RETIRED_PREFIX
    for chunk in _id_chunks(list(session_ids)):
        conn.execute("UPDATE sessions SET parent_session_id=NULL, runtime_revision=runtime_revision+1 "
                     f"WHERE parent_session_id IN ({_placeholders(chunk)})", chunk)
    for sid in session_ids:
        conn.execute("UPDATE state_meta SET value=json_set(value, '$.cleanup', json(?)) WHERE key=?",
                     (json.dumps({"sessions_dir": str(sessions_dir) if sessions_dir is not None else None}),
                      RETIRED_PREFIX + sid))


def _page_ids(db, sid, max_rows=DELETE_ROWS):
    # Enumerate lengths on a reader, outside the writer lock. Retired transcripts
    # reject appends, so another cleaner can only remove these candidates.
    from hermes_state_messages import _MESSAGE_WRITE_COLUMNS
    lengths = " + ".join(f"COALESCE(length(CAST({column} AS BLOB)),0)" for column in _MESSAGE_WRITE_COLUMNS)
    rows = db._read_all(
        f"SELECT id, {lengths} FROM messages WHERE session_id=? ORDER BY id LIMIT ?", (sid, max_rows))
    selected, size = [], 0
    for row_id, row_size in rows:
        row_size = row_size or 0
        if selected and size + row_size > DELETE_BYTES:
            break
        selected.append(row_id)
        size += row_size
    return selected, size


def _delete_page(conn, sid, selected, key, marker):
    from hermes_state_media import retire_spillover
    # A delayed cleaner must not touch a later incarnation after another cleaner
    # completed this marker and its sweep fence eventually expired.
    if conn.execute("SELECT 1 FROM state_meta WHERE key=? AND value=?", (key, marker)).fetchone() is None:
        return None
    if not selected:
        if conn.execute("SELECT 1 FROM messages WHERE session_id=? LIMIT 1", (sid,)).fetchone():
            return True
        conn.execute("UPDATE sessions SET parent_session_id=NULL, runtime_revision=runtime_revision+1 "
                     "WHERE parent_session_id=?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        return False
    started = time.monotonic()
    # Small SQL steps let the time target shorten a page on a contended host.
    for chunk in _id_chunks(selected, size=1):
        retire_spillover(conn, [sid], message_ids=chunk)
        conn.execute(f"DELETE FROM messages WHERE session_id=? AND id IN ({_placeholders(chunk)})", (sid, *chunk))
        if time.monotonic() - started >= DELETE_SECONDS:
            break
    return True


def remove_session_files(sessions_dir, session_id):
    """Sweep historical transcript names lazily, checking the pass before each unlink."""
    import glob
    from itertools import chain
    from hermes_state_prune_budget import check_prune_budget
    if sessions_dir is None:
        return
    targets = [sessions_dir / f"{session_id}{suffix}" for suffix in (".json", ".jsonl")]
    targets.append(sessions_dir / f"session_{session_id}.json")
    try:
        dumps = sessions_dir.glob(f"request_dump_{glob.escape(session_id)}_*.json")
        for path in chain(targets, dumps):
            check_prune_budget()
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def _finish_delete(db, sid, key, value):
    from hermes_state_prune_budget import PruneBudgetExpired, check_prune_budget, current_budget, prune_page_budget
    cleanup = json.loads(value)["cleanup"]
    while True:
        check_prune_budget()
        selected, size = _page_ids(db, sid, max_rows=1 if cleanup.get("attempts") else DELETE_ROWS)
        started = time.monotonic()
        try:
            with prune_page_budget(sid, size if len(selected) == 1 else 0, cleanup.get("attempts", 0)):
                progress = db._execute_write(lambda conn: _delete_page(conn, sid, selected, key, value),
                                             session_id=sid, input_rows=len(selected))
        except PruneBudgetExpired:
            current_budget().pause(min(2.0, max(0.05, time.monotonic() - started)))
            raise
        if not progress:
            break
        pause = min(2.0, max(0.05, time.monotonic() - started))
        if budget := current_budget():
            budget.pause(pause)
        else:
            time.sleep(pause)
    if progress is None:
        return
    check_prune_budget()
    directory = cleanup["sessions_dir"]
    db._remove_session_files(Path(directory) if directory is not None else None, sid)
    db._execute_write(lambda conn: conn.execute(
        "UPDATE state_meta SET value=json_remove(value, '$.cleanup') WHERE key=? AND value=?", (key, value)))


def _defer_expired_delete(db, sid, key, value):
    from hermes_state_prune_budget import current_budget
    budget = current_budget()
    # A pass/window deadline still ends the pass. A page cap only defers this marker.
    budget.check()
    budget.deferred_ids.add(sid)
    db._execute_write(lambda conn: conn.execute(
        "UPDATE state_meta SET value=json_set(value, '$.cleanup.attempts', "
        "COALESCE(json_extract(value, '$.cleanup.attempts'),0)+1, '$.cleanup.last_expired_at', ?) "
        "WHERE key=? AND value=?", (time.time(), key, value)))
    logger.warning("Session cleanup deferred for %r after writer budget expiry; retirement fence retained", sid)


def finish_pending_deletes(db, *, required_ids=()):
    """Finish committed deletes on the next explicit delete/prune, never at read time.

    Each step is idempotent under whole-callback retry or concurrent cleaners.
    A blocked older marker remains fenced and logged without wedging unrelated
    deletes. Errors for the caller's required ids still propagate after the pass.
    """
    from hermes_state_mutation_retirement import RETIRED_PREFIX
    from hermes_state_prune_budget import PruneBudgetExpired, check_prune_budget, current_budget
    check_prune_budget()
    required = set(required_ids)
    first_error = None
    rows = db._read_all(
        "SELECT key,value FROM state_meta WHERE key>=? AND key<? "
        "AND json_valid(value) AND json_type(value, '$.cleanup')='object' "
        "ORDER BY json_extract(value, '$.cleanup.last_expired_at') IS NULL, "
        "json_extract(value, '$.cleanup.last_expired_at'), key",
        prefix_range(RETIRED_PREFIX))
    for key, value in rows:
        sid = key[len(RETIRED_PREFIX):]
        if (budget := current_budget()) is not None and sid in budget.deferred_ids:
            continue
        try:
            _finish_delete(db, sid, key, value)
        except PruneBudgetExpired:
            _defer_expired_delete(db, sid, key, value)
        except sqlite3.IntegrityError as exc:
            # Keep the guard and durable cleanup marker. A legacy late writer or
            # one row's failing trigger must not block every later delete/prune.
            logger.warning("Session cleanup deferred for %r; retirement fence retained", sid, exc_info=True)
            if sid in required and first_error is None:
                first_error = exc
    if rows:
        while db._execute_write(lambda conn: db._delete_unreferenced_system_prompts(conn, limit=100)) == 100:
            time.sleep(0.01)
    if first_error is not None:
        raise first_error
