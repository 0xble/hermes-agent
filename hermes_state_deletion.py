"""Resumable transcript reclamation behind the existing durable retirement fence.

The retirement transaction is the visibility boundary. Message deletion keeps all
FTS delete triggers enabled and yields the writer after a bounded page. No schema
migration, second transcript copy, or independent deletion identity is needed.
"""

import json
import time
from pathlib import Path

from hermes_state_common import _id_chunks, _placeholders
from hermes_state_keys import prefix_range

# A page bounds trigger work by rows AND serialized input. An oversized message
# must make progress alone. SQLite cannot preempt one trigger or an fsync.
DELETE_ROWS = 64
DELETE_BYTES = 256 * 1024
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


def _page_ids(db, sid):
    # Enumerate lengths on a reader, outside the writer lock. Retired transcripts
    # reject appends, so another cleaner can only remove these candidates.
    from hermes_state_messages import _MESSAGE_WRITE_COLUMNS
    lengths = " + ".join(f"COALESCE(length(CAST({column} AS BLOB)),0)" for column in _MESSAGE_WRITE_COLUMNS)
    rows = db._read_all(
        f"SELECT id, {lengths} FROM messages WHERE session_id=? ORDER BY id LIMIT ?", (sid, DELETE_ROWS))
    selected, size = [], 0
    for row_id, row_size in rows:
        row_size = row_size or 0
        if selected and size + row_size > DELETE_BYTES:
            break
        selected.append(row_id)
        size += row_size
    return selected


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
    for chunk in _id_chunks(selected, size=8):
        retire_spillover(conn, [sid], message_ids=chunk)
        conn.execute(f"DELETE FROM messages WHERE session_id=? AND id IN ({_placeholders(chunk)})", (sid, *chunk))
        if time.monotonic() - started >= DELETE_SECONDS:
            break
    return True


def finish_pending_deletes(db):
    """Finish committed deletes on the next explicit delete/prune, never at read time.

    Each step is idempotent under whole-callback retry or concurrent cleaners.
    Errors propagate: a failed cleanup is not a successful empty query result.
    """
    from hermes_state_mutation_retirement import RETIRED_PREFIX
    rows = db._read_all(
        "SELECT key,value FROM state_meta WHERE key>=? AND key<? "
        "AND json_valid(value) AND json_type(value, '$.cleanup')='object'",
        prefix_range(RETIRED_PREFIX))
    for key, value in rows:
        sid = key[len(RETIRED_PREFIX):]
        while True:
            selected = _page_ids(db, sid)
            progress = db._execute_write(lambda conn: _delete_page(conn, sid, selected, key, value),
                                         session_id=sid, input_rows=len(selected))
            if not progress:
                break
            time.sleep(0.01)  # same writer-fairness yield as retention pruning
        if progress is None:
            continue
        directory = json.loads(value)["cleanup"]["sessions_dir"]
        db._remove_session_files(Path(directory) if directory is not None else None, sid)
        db._execute_write(lambda conn: conn.execute(
            "UPDATE state_meta SET value=json_remove(value, '$.cleanup') WHERE key=? AND value=?", (key, value)))
    if rows:
        while db._execute_write(lambda conn: db._delete_unreferenced_system_prompts(conn, limit=100)) == 100:
            time.sleep(0.01)
