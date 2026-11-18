"""Conversation-generation bump for a session-reset boundary.

Moved out of ``hermes_state_messages`` so that facade can stay under its line
cap. Callers pass the SessionDB instance; the SQL lives with the helper.
"""

from __future__ import annotations

from hermes_state_common import _RESET_END_REASONS

_BUMP_GENERATION_SQL = """
            INSERT INTO conversation_generations (source, session_key, generation)
            VALUES (?, ?, 1)
            ON CONFLICT(source, session_key) DO UPDATE
                SET generation = conversation_generations.generation + 1
            """


def bump_conversation_generation(db, conn, session_id: str, end_reason: str) -> None:
    """Advance the peer's conversation generation past a boundary, in the txn that writes it.

    Only ``_RESET_END_REASONS`` count (compression continues one conversation). Never derived
    from session rows (deletes/prunes could re-emit a retired affinity identity); it only ever
    increments.
    """
    del db  # the mixin method took self; callers still pass the SessionDB instance
    if end_reason not in _RESET_END_REASONS:
        return
    row = conn.execute("SELECT source, session_key FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return
    source, session_key = (str(row[k] or "").strip() for k in ("source", "session_key"))
    if source and session_key:
        conn.execute(_BUMP_GENERATION_SQL, (source, session_key))
