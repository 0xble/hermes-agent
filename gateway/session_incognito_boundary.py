"""Atomic exclusion of durable producers before a topic's privacy publication."""
from contextlib import contextmanager


def require_open_database(db):
    from hermes_state import SessionDB
    from hermes_state_runtime import RuntimeStoreError
    if not isinstance(db, SessionDB) or db._conn is None:
        raise RuntimeStoreError('storage_unavailable')


@contextmanager
def idle_privacy_boundary(db, session_id):
    from agent.incognito import privacy_transition
    from hermes_state_runtime import RuntimeStoreError
    from hermes_state_runtime_workers import runtime_lineage, worker_states
    # A JSONL fallback or closed handle cannot prove the absence of durable work.
    # In particular, do not reopen a closed handle under an assumed privacy fence.
    require_open_database(db)
    # Writer order is DB lock -> shared gate. Entering reverses neither order,
    # and only connection-taking reads are allowed until the boundary is published.
    with db._lock, privacy_transition():
        require_open_database(db)
        db._raise_if_db_replaced()
        if worker_states(db._conn, session_id):
            raise RuntimeStoreError('session_busy')
        targets = runtime_lineage(db._conn, session_id)
        for target in targets:
            if db._conn.execute('SELECT 1 FROM session_admissions WHERE target_session_id=? '
                                "AND status IN ('queued','started','unknown')", (target,)).fetchone():
                raise RuntimeStoreError('session_busy')
        yield db._conn


def saved_prefix(db, conn, session_id):
    """Project the settled prefix on the held connection without reacquiring DB/store locks."""
    target = db._compression_tip_on(conn, session_id)
    from hermes_state_deletion import visible_session_sql
    rows = conn.execute(f'SELECT {db._CONVERSATION_ROW_COLUMNS} FROM messages '
                        f'WHERE session_id=? AND {visible_session_sql("session_id")} AND active=1 ORDER BY id',
                        (target,)).fetchall()
    from hermes_state_identity import _newest_message_uid_rows
    rows = _newest_message_uid_rows(rows)
    return db._rows_to_conversation(rows, session_id=target, include_ancestors=False,
        repair_alternation=True, include_row_ids=False, include_summary_markers=True)
