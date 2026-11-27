"""Rows addressed by their ``display_kind``: stamping this turn's freshly flushed row and retiring
every live row of one presentation class. Split out of ``hermes_state_messages`` and bound onto
SessionDB through ``SessionMessagesMixin``; built on its _execute_write/_write_rowcount/_encode_*
primitives."""

from __future__ import annotations

from typing import Any, Optional

from agent.message_sanitization import _sanitize_surrogates
from hermes_state_deletion import visible_session_sql

_VISIBLE_MESSAGE_SQL = visible_session_sql("session_id")


def _scrub_surrogates(value: Any) -> Any:
    """Lone surrogates make sqlite3 raise UnicodeEncodeError and abort the whole write."""
    return _sanitize_surrogates(value) if isinstance(value, str) else value


class SessionDisplayKindMixin:
    """Display-kind-addressed row writes."""

    def set_latest_matching_message_display_kind(self, session_id: str, *, role: str, content: str,
                                                 display_kind: str,
                                                 display_metadata: Optional[dict[str, Any]] = None) -> bool:
        """Stamp presentation metadata on this turn's freshly persisted row (newest active row by content,
        right after the serial turn flushed); the model still sees ``role``/``content`` unchanged, so
        producer provenance survives without classifying by content at render time."""
        if not session_id or not content or not display_kind:
            return False
        def _do(conn):
            row = conn.execute(f"SELECT id FROM messages WHERE {_VISIBLE_MESSAGE_SQL} AND session_id = ? AND role = ? "
                "AND content = ? AND active = 1 ORDER BY id DESC LIMIT 1",
                (session_id, role, self._encode_content(content))).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE messages SET display_kind = ?, display_metadata = ? WHERE id = ?",
                (_scrub_surrogates(display_kind), self._encode_display_metadata(display_metadata), row[0]))
            return True
        return self._execute_write(_do, session_ids=(session_id,), skip_result=False)

    def deactivate_messages_by_display_kind(self, session_id: str, display_kind: str) -> int:
        """Deactivate every live row of one ``display_kind`` (idempotent; returns the affected row count).
        The durable counterpart to the in-memory strip a self-replacing pivot performs: the in-memory path
        drops the prior entry so N pivots leave one, but a durable append has no such step, so every switch
        left another active row and all of them replayed on resume. Rows are preserved (inactive), never
        deleted — the same contract as :meth:`deactivate_message`, keyed by class instead of by id.
        """
        if not session_id or not display_kind:
            return 0
        return self._write_rowcount(
            "UPDATE messages SET active = 0 WHERE session_id = ? AND display_kind = ? AND active = 1",
            (session_id, _scrub_surrogates(display_kind)), session_ids=(session_id,))
