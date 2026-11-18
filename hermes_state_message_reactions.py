"""Message reaction storage and next-turn announcements on the visible session lineage."""

from __future__ import annotations

from typing import Any, Optional


class SessionMessagesMixin:
    def _reaction_list(self, meta: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
        """Well-formed (dict) reactions stored under ``REACTIONS_METADATA_KEY``."""
        reactions = (meta or {}).get(self.REACTIONS_METADATA_KEY)
        return [r for r in reactions if isinstance(r, dict)] if isinstance(reactions, list) else []


    def set_message_reaction(self, session_id: str, message_row_id: int, emoji: Optional[str], *,
                             author: str = "user") -> Optional[list[dict[str, Any]]]:
        """Set (``emoji=None``: clear) *author*'s reaction. Tapback semantics: one per author per message;
        the same emoji again clears, a different one replaces. Returns the list after the write, or
        ``None`` for a row outside the session's visible resume lineage (see ``_reaction_row_query``)."""
        from hermes_state_messages import _SET_DISPLAY_META_SQL, _scrub_surrogates, time
        if not session_id or message_row_id is None:
            return None
        sql, params = self._reaction_row_query(session_id, message_row_id)
        def _do(conn):
            row = conn.execute(sql, params).fetchone()
            if row is None:
                return None
            meta = self._decode_display_metadata(row[0]) or {}
            existing = self._reaction_list(meta)
            reactions = [r for r in existing if r.get("author") != author]
            previous = next((r for r in existing if r.get("author") == author), None)
            if emoji and (previous is None or previous.get("emoji") != emoji):
                reactions.append({"emoji": _scrub_surrogates(emoji), "author": author, "at": time.time()})
            if reactions:
                meta[self.REACTIONS_METADATA_KEY] = reactions
            else:
                meta.pop(self.REACTIONS_METADATA_KEY, None)
            conn.execute(_SET_DISPLAY_META_SQL, (self._encode_display_metadata(meta) if meta else None, message_row_id))
            return reactions
        return self._execute_write(_do, session_ids=(session_id,), skip_result=[])


    def get_message_reactions(self, session_id: str, message_row_id: int) -> list[dict[str, Any]]:
        """Reaction list persisted on one message row (never ``None``)."""
        if not session_id or message_row_id is None:
            return []
        row = self._read_one(*self._reaction_row_query(session_id, message_row_id))
        return self._reaction_list(self._decode_display_metadata(row[0])) if row is not None else []


    def _reaction_row_query(self, session_id: str, message_row_id: int) -> tuple[str, tuple]:
        """A reaction addresses a row the client can SEE, and a display resume materializes the whole
        compression lineage (active + compacted rows, with row ids) — so a row is "in this session" when
        its owner is any lineage segment, not only the tip, and a rewound row is not. Explicit ``/branch``
        copies keep their own rows (``_resume_lineage_ids``)."""
        from hermes_state_messages import _DISPLAY_META_ROW_SQL, _placeholders
        lineage = self._resume_lineage_ids(session_id)
        return _DISPLAY_META_ROW_SQL.format(ids=_placeholders(lineage)), (message_row_id, *lineage)


    def take_unseen_reactions(self, session_id: str, *, author: str = "user") -> list[dict[str, Any]]:
        """Return *author*'s not-yet-surfaced reactions and mark them seen. Reactions are announced on the
        NEXT user turn (never by rewriting the reacted message: cache-safe); ``seen`` makes it exactly once.
        Include compaction-archived history that remains visible, but exclude rewound/superseded rows."""
        from hermes_state_messages import (
            _DISPLAY_ACTIVE_CLAUSE, _SET_DISPLAY_META_SQL, _VISIBLE_MESSAGE_SQL, _placeholders,
            _sql_json_extract,
        )
        if not session_id:
            return []
        lineage = self._resume_lineage_ids(session_id)
        def _do(conn):
            pending = []
            # Only reaction-bearing rows cross into Python: display_metadata also carries delivery /
            # attachment markers on most rows, and the lineage scan grows with the session's age.
            for row in conn.execute("SELECT id, role, content, display_metadata FROM messages "
                    f"WHERE {_VISIBLE_MESSAGE_SQL} AND session_id IN ({_placeholders(lineage)}){_DISPLAY_ACTIVE_CLAUSE} "
                    f"AND {_sql_json_extract('display_metadata', '$.' + self.REACTIONS_METADATA_KEY)} IS NOT NULL "
                    "ORDER BY id", tuple(lineage)).fetchall():
                meta = self._decode_display_metadata(row["display_metadata"])
                reactions = meta.get(self.REACTIONS_METADATA_KEY) if meta else None
                if not isinstance(reactions, list):
                    continue
                changed = False
                for reaction in reactions:
                    if not isinstance(reaction, dict) or reaction.get("author") != author or reaction.get("seen"):
                        continue
                    reaction["seen"] = True
                    changed = True
                    content = self._decode_content(row["content"])
                    pending.append({
                        "row_id": row["id"], "role": row["role"], "emoji": reaction.get("emoji") or "",
                        "text": content if isinstance(content, str) else ""})
                if changed:
                    conn.execute(_SET_DISPLAY_META_SQL, (self._encode_display_metadata(meta), row["id"]))
            return pending
        return self._execute_write(_do, session_ids=(session_id,), skip_result=[])
