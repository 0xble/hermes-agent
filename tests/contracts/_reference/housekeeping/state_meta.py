"""In-memory reference implementation of ``StateMetaRepository`` (test fixture).

Registered onto the assembled backend as the ``state_meta`` attribute per the append-only extension contract in ``backend.py``. Keys live under the ``"state_meta"`` key of the owning scope's namespace, so cross-scope isolation is structural. The sessions reference reads the same table for the retag gate (``retag_worker_sessions``), which is why the table name is shared through :func:`state_meta_table` instead of duplicated.
"""

from __future__ import annotations

import string
from collections.abc import Callable
from contextlib import contextmanager
from typing import Dict, Optional

from persistence import ProfileScope
from persistence.conversation.messages import RowVisibility
from persistence.housekeeping.state_meta import StateMetaEntry

from ..backend import InMemoryBackend, register_repository

__all__ = ["InMemoryStateMetaRepository", "state_meta_table"]

_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _fold(text: str) -> str:
    """ASCII letters lowered, every other character kept: the case rule of SQLite's built-in ``LIKE``."""
    return text.translate(_ASCII_LOWER)


def state_meta_table(backend: InMemoryBackend, scope: ProfileScope) -> dict[str, str]:
    """The scope's meta table, created on first touch; shared with the sessions reference's retag gate."""
    table: dict[str, str] = backend.scope_store(scope).setdefault("state_meta", {})
    return table


def require_key(key: str) -> None:
    """The boundary's input-shape check: an empty key is a caller error, never a store row."""
    if not key:
        raise ValueError("state_meta key must be non-empty")


class _ReferenceLifecycle:
    def __init__(self, store, table):
        self.store = store
        self.table = table
        self.sessions = store.scope_store(store.scope).get("sessions", {})

    @staticmethod
    def _check_key(key):
        family, _, member = key.partition(":")
        if family not in {"goal", "loop", "session_control"} or not member:
            raise ValueError("invalid lifecycle key")

    def get(self, key):
        self._check_key(key)
        return self.table.get(key)

    def set(self, key, value):
        self._check_key(key)
        self.table[key] = value

    @contextmanager
    def savepoint(self):
        before = self.table.copy()

        def rollback():
            self.table.clear()
            self.table.update(before)

        try:
            yield rollback
        except BaseException:
            rollback()
            raise

    def title(self, session_id):
        return self.sessions.get(session_id, {}).get("title")

    def other_titles(self, session_id):
        return tuple(row["title"] for sid, row in self.sessions.items() if sid != session_id and row["title"])

    def user_messages(self, session_id, *, since=0, until=0, limit=500, before_id=0, current_only=False):
        from ..conversation.transcript_store import rows_of

        results = []
        for _, row in reversed(rows_of(self.store, self.store.scope, (session_id,))):
            msg = row.message
            if (msg.role != "user" or row.epoch < since or (until > 0 and row.epoch >= until)
                    or (before_id > 0 and row.id >= before_id)
                    or (current_only and row.visibility is RowVisibility.REWOUND)):
                continue
            results.append({"id": row.id, "content": msg.content, "timestamp": row.epoch,
                            "display_kind": msg.display_kind, "compressed_summary": msg.compressed_summary})
            if len(results) >= max(1, limit):
                break
        return results

    def _compression_parent(self, session_id):
        from ..conversation.lineage import _compression_child, _non_continuation

        row = self.sessions.get(session_id)
        if row is None or not _compression_child(self.sessions, row):
            return None
        parent = row["parent_session_id"]
        return None if _non_continuation(row, parent) else parent

    def has_compression_ancestor(self, session_id, ancestor_id):
        for _ in range(64):
            session_id = self._compression_parent(session_id)
            if session_id is None:
                break
            if session_id == ancestor_id:
                return True
        return False

    def compression_successor(self, session_id):
        current, seen = session_id, {session_id}
        for _ in range(64):
            children = [sid for sid in self.sessions if self._compression_parent(sid) == current]
            if not children:
                return current if current != session_id else None
            if len(children) != 1 or children[0] in seen:
                return None
            current = children[0]
            seen.add(current)
        return None


class InMemoryStateMetaRepository:
    """See module docstring; semantics follow persistence/housekeeping/state_meta.py exactly."""

    def __init__(self, backend: InMemoryBackend) -> None:
        self._backend = backend

    def get(self, key: str) -> Optional[str]:
        scope = self._backend.scope
        require_key(key)
        return state_meta_table(self._backend, scope).get(key)

    def set(self, key: str, value: str) -> None:
        scope = self._backend.scope
        require_key(key)
        state_meta_table(self._backend, scope)[key] = value

    def update(
        self, key: str, decide: Callable[[Optional[str]], Optional[str]],
    ) -> tuple[bool, Optional[str]]:
        require_key(key)
        table = state_meta_table(self._backend, self._backend.scope)
        before = table.get(key)
        after = decide(before)
        if before == after:
            return False, after
        if after is None:
            del table[key]
        else:
            table[key] = after
        return True, after

    def list_prefix(self, prefix: str) -> tuple[StateMetaEntry, ...]:
        scope = self._backend.scope
        require_key(prefix)
        table = state_meta_table(self._backend, scope)
        folded = _fold(prefix)
        # Insertion order (``set`` on a stored key keeps its place), as the SQLite table scan reads.
        return tuple(StateMetaEntry(key=key, value=value) for key, value in table.items() if _fold(key[:len(prefix)]) == folded)

    def read_lifecycle(self, read):
        return read(_ReferenceLifecycle(self._backend, state_meta_table(self._backend, self._backend.scope).copy()))

    def write_lifecycle(self, write):
        table = state_meta_table(self._backend, self._backend.scope)
        staged = table.copy()
        result = write(_ReferenceLifecycle(self._backend, staged))
        table.clear()
        table.update(staged)
        return result


register_repository("state_meta", InMemoryStateMetaRepository)
