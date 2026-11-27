"""``StateMetaRepository`` on SQLite: the ``state_meta`` key/value rows, read and written through the store holder's
own ``get_meta``, ``set_meta`` and ``list_meta_prefix``."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Optional, TypeVar

from persistence.housekeeping.state_meta import LifecycleReader, LifecycleWriter, StateMetaEntry

from .._backend import SQLiteStore, register_repository, translated

__all__ = ["SQLiteStateMetaRepository"]

_T = TypeVar("_T")


class _SQLiteLifecycle:
    """Scoped lifecycle operations, without exposing the shared writer's SQL connection."""

    def __init__(self, conn) -> None:
        self._conn = conn

    @staticmethod
    def _check_key(key: str) -> None:
        if key.partition(":")[0] not in {"goal", "loop", "session_control"} or not key.partition(":")[2]:
            raise ValueError("invalid lifecycle key")

    def get(self, key: str) -> Optional[str]:
        self._check_key(key)
        row = self._conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def set(self, key: str, value: str) -> None:
        self._check_key(key)
        self._conn.execute(
            """INSERT INTO state_meta(key,value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value),
        )

    @contextmanager
    def savepoint(self):
        self._conn.execute("SAVEPOINT lifecycle_change")

        def rollback():
            self._conn.execute("ROLLBACK TO lifecycle_change")

        try:
            yield rollback
        except BaseException:
            rollback()
            raise
        finally:
            self._conn.execute("RELEASE lifecycle_change")

    def title(self, session_id: str) -> Optional[str]:
        row = self._conn.execute("SELECT title FROM sessions WHERE id=?", (session_id,)).fetchone()
        return None if row is None else row[0]

    def other_titles(self, session_id: str) -> tuple[str, ...]:
        return tuple(row[0] for row in self._conn.execute(
            "SELECT title FROM sessions WHERE id!=? AND title IS NOT NULL AND title!=''", (session_id,),
        ))

    def user_messages(self, session_id, *, since=0, until=0, limit=500, before_id=0, current_only=False):
        from persistence.sqlite.conversation._store.retirement import visible_session_sql
        from persistence.sqlite.conversation._store.messages_sql import _DISPLAY_ACTIVE_CLAUSE

        sql = f"""SELECT id, content, timestamp, display_kind, _compressed_summary FROM messages
                  WHERE {visible_session_sql("session_id")} AND session_id=? AND role='user' AND timestamp>=?"""
        params = [session_id, since]
        if until > 0:
            sql += " AND timestamp<?"
            params.append(until)
        if before_id > 0:
            sql += " AND id<?"
            params.append(before_id)
        if current_only:
            sql += _DISPLAY_ACTIVE_CLAUSE
        return [{"id": row[0], "content": row[1], "timestamp": row[2], "display_kind": row[3],
                 "compressed_summary": bool(row[4])}
                for row in self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, max(1, limit)))]

    def compression_successor(self, session_id: str) -> Optional[str]:
        from persistence.sqlite._schema_ddl import _COMPRESSION_CHILD_SQL
        from persistence.sqlite.conversation._store.sql_common import _non_continuation_child_sql

        sql = (f"SELECT c.id FROM sessions c WHERE c.parent_session_id=? "
               f"AND {_COMPRESSION_CHILD_SQL.format(a='c')} "
               f"{_non_continuation_child_sql('c.', 'c.parent_session_id')} LIMIT 2")
        current, seen = session_id, {session_id}
        for _ in range(64):
            rows = self._conn.execute(sql, (current,)).fetchall()
            if not rows:
                return current if current != session_id else None
            if len(rows) > 1 or rows[0][0] in seen:
                return None
            current = rows[0][0]
            seen.add(current)
        return None

    def has_compression_ancestor(self, session_id: str, ancestor_id: str) -> bool:
        from persistence.sqlite._schema_ddl import _COMPRESSION_CHILD_SQL
        from persistence.sqlite.conversation._store.sql_common import _non_continuation_child_sql

        row = self._conn.execute(
            f"""WITH RECURSIVE lineage(id, depth) AS (
                SELECT ?, 0 UNION ALL
                SELECT c.parent_session_id, lineage.depth + 1
                FROM lineage JOIN sessions c ON c.id = lineage.id
                WHERE lineage.depth < 64 AND {_COMPRESSION_CHILD_SQL.format(a='c')}
                {_non_continuation_child_sql('c.', 'c.parent_session_id')}
            ) SELECT 1 FROM lineage WHERE id=? AND depth>0 LIMIT 1""", (session_id, ancestor_id),
        ).fetchone()
        return row is not None


class SQLiteStateMetaRepository:
    """Core-owned bookkeeping keys of the bound profile's ``state.db``."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def get(self, key: str) -> Optional[str]:
        if not key:
            raise ValueError("state_meta key must be non-empty")
        with translated():
            return self._store.holder().get_meta(key)

    def set(self, key: str, value: str) -> None:
        if not key:
            raise ValueError("state_meta key must be non-empty")
        with translated():
            self._store.holder().set_meta(key, value)

    def update(
        self, key: str, decide: Callable[[Optional[str]], Optional[str]],
    ) -> tuple[bool, Optional[str]]:
        if not key:
            raise ValueError("state_meta key must be non-empty")
        with translated():
            holder = self._store.holder()

            def write(conn):
                row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
                before = None if row is None else row[0]
                after = decide(before)
                if before == after:
                    return False, after
                if after is None:
                    conn.execute("DELETE FROM state_meta WHERE key=?", (key,))
                else:
                    holder.set_meta(key, after, cursor=conn)
                return True, after

            return holder._execute_write(write)

    def list_prefix(self, prefix: str) -> tuple[StateMetaEntry, ...]:
        if not prefix:
            raise ValueError("state_meta prefix must be non-empty")
        with translated():
            rows = self._store.holder().list_meta_prefix(prefix)
        # The store's LIKE folds ASCII case and reads in table order.
        return tuple(StateMetaEntry(key, value) for key, value in rows)

    def read_lifecycle(self, read: Callable[[LifecycleReader], _T]) -> _T:
        with translated(), self._store.holder()._read_ctx() as conn:
            if conn.in_transaction:
                return read(_SQLiteLifecycle(conn))
            conn.execute("BEGIN")
            try:
                return read(_SQLiteLifecycle(conn))
            finally:
                conn.rollback()

    def write_lifecycle(self, write: Callable[[LifecycleWriter], _T]) -> _T:
        with translated():
            return self._store.holder()._execute_write(lambda conn: write(_SQLiteLifecycle(conn)))


register_repository("state_meta", SQLiteStateMetaRepository)
