"""Display-fold planning, affected-group closure, and the full-fold oracle."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import sqlite3
from typing import Optional

from hermes_state_common import _placeholders
from hermes_state_publication import state_lock
from hermes_state_write_metrics import add_write_rows, timed_write_phase, write_phase

logger = logging.getLogger("hermes_state")
_Freshness = tuple[sqlite3.Connection, int, int]
_VISIBLE = " AND (active = 1 OR compacted = 1)"
_DISPLAY_FOLD_COLUMNS = ("id", "role", "content", "timestamp", "tool_call_id", "tool_calls", "tool_name",
                         "display_kind", "display_metadata", "display_order", "display_identity")
_DISPLAY_FOLD_COLUMNS_SQL = f"SELECT {', '.join(_DISPLAY_FOLD_COLUMNS)} FROM messages"
_CARRIER_FOLD_COLUMNS = (*_DISPLAY_FOLD_COLUMNS, "message_uid", "_compressed_summary")
_CARRIER_FOLD_COLUMNS_SQL = f"SELECT {', '.join(_CARRIER_FOLD_COLUMNS)} FROM messages"


def _exact_cell(column: str) -> str:
    """One column as JSON that decodes to the exact value and type ``sqlite3`` returns. REAL goes
    through ``%!.17g`` (the runtime's JSON REAL rendering keeps only 15 digits); a REAL zero is
    flagged because neither form keeps its sign."""
    return (f"CASE typeof({column}) WHEN 'blob' THEN json_object('b', hex({column})) "
            f"WHEN 'real' THEN CASE WHEN {column} = 0 THEN json_object('z', 0) "
            f"ELSE json_object('r', printf('%!.17g', {column})) END "
            f"ELSE json_array({column}) END")


def _decode_cell(cell):
    if isinstance(cell, list):
        return cell[0]
    if "b" in cell:
        return bytes.fromhex(cell["b"])
    return float(cell["r"])


def one_step_rows(conn: sqlite3.Connection, select_sql: str, params, columns: tuple[str, ...]) -> list[tuple]:
    """The rows of *select_sql* (selecting *columns*), fetched in ONE SQLite step, in no set order.

    Each row a cursor yields releases and re-takes the GIL. With the gateway's busy threads every
    re-take waits a switch interval, so the off-lock plan's ~17k rows took 80 s behind a single
    CPU-bound thread (0.5 s alone) and pinned a compression worker past its ceiling, and the
    in-lock 19k-row display index held the writer 5-9 s (2026-10-25). A page holding a REAL zero
    is refetched conventionally to keep the zero's sign.
    """
    cells = ", ".join(_exact_cell(column) for column in columns)
    page = conn.execute(f"SELECT json_group_array(json_array({cells})) FROM ({select_sql})", params).fetchone()[0]
    rows = []
    for raw in json.loads(page):
        if any(isinstance(cell, dict) and "z" in cell for cell in raw):
            return [tuple(row) for row in conn.execute(select_sql, params)]
        rows.append(tuple(map(_decode_cell, raw)))
    return rows


def fold_rows(conn: sqlite3.Connection, select_sql: str, params,
              columns: tuple[str, ...] = _DISPLAY_FOLD_COLUMNS) -> list[dict]:
    """Rows of a ``_DISPLAY_FOLD_COLUMNS_SQL`` query as dicts in id order, in one SQLite step."""
    rows = [dict(zip(columns, row))
            for row in one_step_rows(conn, select_sql, params, columns)]
    rows.sort(key=lambda row: row["id"])
    return rows


_DISPLAY_IDENTITY_INDEX_SQL = ("SELECT id, display_identity, display_order FROM messages "
                              "INDEXED BY idx_messages_display_identity WHERE session_id = ? "
                              "AND display_identity IS NOT NULL" + _VISIBLE)
_DISPLAY_BACKFILL_INDEX_SQL = ("SELECT id, display_identity, display_order FROM messages "
                              "INDEXED BY idx_messages_display_backfill WHERE session_id = ? "
                              "AND (display_order IS NULL OR display_identity IS NULL)" + _VISIBLE)


_PAYLOAD_PAGE_BYTES = 1024 * 1024
_PAYLOAD_SIZE_SQL = " + ".join(
    f"COALESCE(length(CAST(m.{column} AS BLOB)), 0)" for column in _CARRIER_FOLD_COLUMNS)


def _payload_page(conn, where_sql, params, *, by_session=True):
    # Retain only ids and byte counts in SQLite. Stop before reading another payload
    # once the budget is consumed, but always admit one oversized row. Materializing
    # 200 multi-MB messages at once regresses legacy display reads' memory bound.
    index = "INDEXED BY idx_messages_session_id " if by_session else ""
    page = json.loads(conn.execute(
        "WITH RECURSIVE eligible(id) AS MATERIALIZED ("
        "SELECT id FROM messages " + index + "WHERE " + where_sql +
        " ORDER BY id LIMIT 200), page(id, size) AS ("
        f"SELECT m.id, {_PAYLOAD_SIZE_SQL} FROM messages m "
        "WHERE m.id = (SELECT MIN(id) FROM eligible) UNION ALL "
        f"SELECT m.id, page.size + {_PAYLOAD_SIZE_SQL} FROM page JOIN messages m "
        "ON m.id = (SELECT MIN(id) FROM eligible WHERE id > page.id) "
        f"WHERE page.size + {_PAYLOAD_SIZE_SQL} <= ?) "
        "SELECT json_group_array(json_array(id, size)) FROM page",
        (*params, _PAYLOAD_PAGE_BYTES)).fetchone()[0])
    if not page:
        return []
    ids = [row[0] for row in page]
    if len(page) == 1 and page[0][1] > _PAYLOAD_PAGE_BYTES:
        # Do not expand one very large string/BLOB through JSON just to batch one row.
        row = conn.execute(_CARRIER_FOLD_COLUMNS_SQL + " WHERE id = ?", ids).fetchone()
        return [dict(row)]
    return fold_rows(conn, f"{_CARRIER_FOLD_COLUMNS_SQL} WHERE id IN ({_placeholders(ids)})",
                     ids, _CARRIER_FOLD_COLUMNS)


def _stale_payload_rows(conn, row_ids):
    row_ids = sorted(row_ids)
    for start in range(0, len(row_ids), 200):
        pending = row_ids[start:start + 200]
        while pending:
            rows = _payload_page(conn, f"id IN ({_placeholders(pending)})", pending, by_session=False)
            if not rows:
                break
            yield from rows
            pending = [row_id for row_id in pending if row_id > rows[-1]["id"]]


def _apply_display_updates(conn, updates):
    # One SQLite step per page, not per UPDATE. Bound parameters below the legacy
    # 999-variable limit. UPDATE FROM preserves BLOB identities without JSON/text coercion.
    for start in range(0, len(updates), 200):
        page = updates[start:start + 200]
        values = ",".join("(?,?,?)" for _ in page)
        cursor = conn.execute(
            "UPDATE messages SET display_order = repairs.column1, display_identity = repairs.column2 "
            f"FROM (VALUES {values}) AS repairs WHERE messages.id = repairs.column3",
            tuple(value for row in page for value in row))
        add_write_rows("display_reconciled", cursor.rowcount)


class _FoldPlan(dict[int, tuple[bytes | None, bytes, int | None]]):
    """Snapshot rows: (stored identity, canonical identity, stored order)."""

    def __init__(self, freshness: _Freshness | None = None):
        super().__init__()
        self.freshness = freshness
        self.aliases: dict[bytes, set[bytes]] = {}
        self.repair_keys: set[bytes] = set()

    def finish(self):
        first: dict[bytes, int] = {}
        for row_id, (stored, canonical, order) in self.items():
            kept = order if stored == canonical and order is not None and order <= row_id else row_id
            minimum = first.setdefault(canonical, kept)
            if stored is not None:
                self.aliases.setdefault(canonical, set()).add(stored)
            if stored != canonical or order != minimum:
                self.repair_keys.add(canonical)


@dataclass
class _AffectedFold:
    plan: _FoldPlan
    keys: set[bytes]
    insert_after: int


def _freshness(conn: sqlite3.Connection | None) -> _Freshness | None:
    if conn is None:
        return None
    row = conn.execute("PRAGMA main.data_version").fetchone()
    if row is None or not isinstance(row[0], int):
        return None
    return conn, conn.total_changes, row[0]


class DisplayFoldMixin:
    def _display_fold_plan(self, session_id: str) -> Optional[_FoldPlan]:
        """``{row id: (stored identity, recomputed identity, order)}`` for every retained row of
        *session_id*, read from one pooled-reader snapshot WITHOUT the writer lock.

        Recomputing the display key decodes every row the session ever compacted (thousands of
        rows and megabytes of content for a long topic session). Under the writer lock that read
        stalled every other writer on a loaded disk (2026-10-11 10:57: 21 turns queued behind an
        ``archive_and_compact``). The plan is only a hint: :meth:`_reconcile_display_orders`
        reuses a recomputed identity only while the row's stored identity still equals the one
        this snapshot saw. A content, role, timestamp, tool or display_kind change in between
        nulls the stored identity (``messages_display_identity_update``). User handoff keys use
        only the live view's content, not its presentation metadata. Metadata-only edits do
        not invalidate that identity, and display visibility is filtered separately on read.
        Every changed row and every row the snapshot did not see is recomputed under the lock.
        Alias/repair discovery is also off-lock. A writer freshness token gates affected-group
        enumeration, but generic/stale-plan callers retain the whole-index fold.
        ``None`` (no WAL reader, a read-only store, any read error) keeps the full in-lock scan."""
        if not session_id or getattr(self, "read_only", False) or not getattr(self, "_wal_active", False):
            return None
        try:
            # Capture BEFORE the reader snapshot. Capturing after it could bless a write
            # that the snapshot cannot see. Use the same publication admission guard.
            with state_lock(self._lock, db_path=self.db_path):
                plan = _FoldPlan(_freshness(self._conn))
            with self._read_ctx() as conn:
                if conn is self._conn:  # degraded read path runs on the writer under its lock
                    return None
                conn.execute("BEGIN")
                try:
                    last_id = 0
                    uid_identity: dict[str, bytes] = {}
                    while True:
                        rows = _payload_page(conn, "session_id = ? AND id > ?" + _VISIBLE, (session_id, last_id))
                        if not rows:
                            break
                        for row in rows:
                            plan[row["id"]] = (
                                row["display_identity"], self._fold_summary_carrier(
                                    row, self._display_identity(self._display_dedupe_key(row)), uid_identity),
                                row["display_order"])
                        last_id = rows[-1]["id"]
                finally:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            logger.debug("display fold plan for %s unavailable, folding under the lock: %s", session_id, exc)
            return None
        plan.finish()
        return plan

    @timed_write_phase("display_index")
    def _visible_display_index(self, conn, session_id: str) -> list[tuple[int, Optional[bytes], Optional[int]]]:
        """Retained active/compacted row identities in id order, without reading message payloads."""
        rows: dict[int, tuple[int, Optional[bytes], Optional[int]]] = {}
        for name, sql in (("display_identity", _DISPLAY_IDENTITY_INDEX_SQL),
                          ("display_backfill", _DISPLAY_BACKFILL_INDEX_SQL)):
            with write_phase(f"{name}_enumerate", thread_cpu=True):
                fetched = one_step_rows(conn, sql, (session_id,), ("id", "display_identity", "display_order"))
                for row in fetched:
                    rows[row[0]] = row
                add_write_rows(f"{name}_rows", len(fetched))
        with write_phase("display_materialize", thread_cpu=True):
            add_write_rows("display_materialized_rows", len(rows))
            return [rows[row_id] for row_id in sorted(rows)]

    @timed_write_phase("display_reconcile")
    def _reconcile_display_orders(self, conn, session_id: str,
                                  plan: _FoldPlan | _AffectedFold | None = None) -> None:
        """Re-fold split display generations by the recomputed display key.

        A compaction generation that computed the display identity differently (or cloned
        without inheriting it) leaves one logical message in two ``display_order`` groups,
        and ``GROUP BY display_order`` then projects it twice (#122167). Folding by the same
        recomputed key :meth:`_dedupe_display_generations` uses keeps every display projection
        on one definition of a logical message; the live copy wins its group via the read
        path's ``ORDER BY candidate.active DESC, candidate.id DESC``. Writes only on drift.

        With a *plan* from :meth:`_display_fold_plan`, rows the snapshot already recomputed
        are folded from narrow identity/order queries, and only new or changed rows have their
        payloads read and recomputed here. The result is the same fold as the full scan below."""
        if isinstance(plan, _AffectedFold):
            try:
                visible, identities = self._affected_display_index(conn, session_id, plan)
            except sqlite3.OperationalError as exc:
                logger.debug("affected display index unavailable for %s, full index: %s", session_id, exc)
                plan = plan.plan
            else:
                add_write_rows("display_incremental", 1)
                self._fold_display_orders(conn, visible, plan.plan, identities)
                return
        add_write_rows("display_fallback", 1)
        if plan is not None:
            try:
                visible = self._visible_display_index(conn, session_id)
            except sqlite3.DatabaseError as exc:  # a display index is missing (or a page too long): full scan
                logger.debug("display index read failed for %s, full fold: %s", session_id, exc)
            else:
                self._fold_display_orders(conn, visible, plan)
                return
        with write_phase("display_full_fold"):
            first_id: dict[bytes, int] = {}
            uid_identity: dict[str, bytes] = {}
            last_id = 0
            while True:
                # Bound payload materialization like the off-lock plan. Streaming a row at a
                # time releases/reacquires the GIL for every retained historical message.
                rows = _payload_page(conn, "session_id = ? AND id > ?" + _VISIBLE, (session_id, last_id))
                batch_start = last_id
                updates = []
                for row in rows:
                    last_id = row["id"]
                    identity = self._fold_summary_carrier(
                        row, self._display_identity(self._display_dedupe_key(row)), uid_identity)
                    kept = row["display_order"] if row["display_identity"] == identity else None
                    order = first_id.setdefault(identity, min(last_id, kept) if kept is not None else last_id)
                    if order != row["display_order"] or identity != row["display_identity"]:
                        updates.append((order, identity, last_id))
                if last_id == batch_start:
                    break
                _apply_display_updates(conn, updates)

    def _resolve_display_identities(self, conn, visible, plan):
        """Stored equality validates payload reuse, not membership or presentation metadata."""
        stale = [row_id for row_id, stored, _order in visible
                 if stored is None or plan.get(row_id, (None,))[0] != stored]
        fresh: dict[int, bytes] = {}
        add_write_rows("display_stale", len(stale))
        with write_phase("display_stale_decode"):
            for row in _stale_payload_rows(conn, stale):
                identity = self._display_identity(self._display_dedupe_key(row))
                if row["message_uid"] and row["_compressed_summary"]:
                    original = conn.execute(
                        _CARRIER_FOLD_COLUMNS_SQL + " WHERE session_id = "
                        "(SELECT session_id FROM messages WHERE id = ?) AND message_uid = ? "
                        "AND id < ?" + _VISIBLE + " ORDER BY id LIMIT 1",
                        (row["id"], row["message_uid"], row["id"])).fetchone()
                    if original is not None:
                        identity = self._display_identity(self._display_dedupe_key(original))
                fresh[row["id"]] = identity
        return {row_id: fresh[row_id] if row_id in fresh else plan[row_id][1]
                for row_id, _stored, _order in visible}

    @timed_write_phase("display_planned_fold")
    def _fold_display_orders(self, conn, visible, plan, identities=None) -> None:
        """Fold complete canonical groups, ordered by their surviving row ids."""
        if identities is None:
            identities = self._resolve_display_identities(conn, visible, plan)
        first_id: dict[bytes, int] = {}
        updates = []
        for row_id, stored, order in visible:
            identity = identities[row_id]
            # Pruning keeps an archived original and its rewritten live row in one historical slot.
            # Preserve a valid earlier slot, but repair future/foreign slots just like the full fold.
            kept = order if stored == identity and order is not None and order <= row_id else row_id
            folded = first_id.setdefault(identity, kept)
            if folded != order or identity != stored:
                updates.append((folded, identity, row_id))
        if updates:
            with write_phase("display_updates"):
                _apply_display_updates(conn, updates)

    @timed_write_phase("display_freshness")
    def _begin_display_fold(self, conn, plan):
        """Called after BEGIN IMMEDIATE, before any compaction mutation, on every retry."""
        if plan is None or plan.freshness is None:
            return plan
        try:
            current = _freshness(conn)
            if (current is None or current[0] is not plan.freshness[0]
                    or current[1:] != plan.freshness[1:]):
                return plan
        except sqlite3.Error as exc:
            logger.debug("display freshness check unavailable, full index: %s", exc)
            return plan
        return _AffectedFold(plan, set(plan.repair_keys),
                             conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])

    @timed_write_phase("display_preimages")
    def _seed_display_removals(self, conn, session_id, selection, row_ids):
        if not isinstance(selection, _AffectedFold):
            return
        for start in range(0, len(row_ids), 500):
            chunk = row_ids[start:start + 500]
            rows = one_step_rows(conn,
                "SELECT id, display_identity, display_order FROM messages "
                f"WHERE session_id = ? AND id IN ({_placeholders(chunk)})" + _VISIBLE,
                (session_id, *chunk), ("id", "display_identity", "display_order"))
            add_write_rows("display_preimage_rows", len(rows))
            identities = self._resolve_display_identities(conn, rows, selection.plan)
            selection.keys.update(identities.values())
            selection.keys.update(row[1] for row in rows if row[1] is not None)

    @timed_write_phase("display_index")
    def _affected_display_index(self, conn, session_id, selection):
        """Close affected canonical groups over their stored aliases, without a history scan."""
        plan = selection.plan
        pending: set[bytes] = set()
        queried: set[bytes] = set()
        expanded: set[bytes] = set()
        selected = {}
        identities = {}

        def enqueue(key):
            aliases = [key]
            while aliases:
                key = aliases.pop()
                if key not in queried:
                    pending.add(key)
                if key not in expanded:
                    expanded.add(key)
                    aliases.extend(plan.aliases.get(key, ()))

        for key in selection.keys:
            enqueue(key)

        def include(rows):
            # Backfill and identity cursors can overlap. Decode a selected payload once.
            unseen = [tuple(row) for row in rows if row[0] not in selected]
            resolved = self._resolve_display_identities(conn, unseen, plan)
            for row in unseen:
                selected[row[0]] = row
                canonical = resolved[row[0]]
                identities[row[0]] = canonical
                enqueue(canonical)
                if row[1] is not None:
                    enqueue(row[1])

        # Actual stored values, not input dict keys or copied raw clone identities.
        with write_phase("display_new_enumerate", thread_cpu=True):
            rows = one_step_rows(conn,
                "SELECT id, display_identity, display_order FROM messages "
                "INDEXED BY idx_messages_session_id WHERE session_id = ? AND id > ?" + _VISIBLE,
                (session_id, selection.insert_after), ("id", "display_identity", "display_order"))
            add_write_rows("display_new_rows", len(rows))
            include(rows)
        with write_phase("display_backfill_enumerate", thread_cpu=True):
            rows = one_step_rows(conn, _DISPLAY_BACKFILL_INDEX_SQL, (session_id,),
                                 ("id", "display_identity", "display_order"))
            add_write_rows("display_backfill_rows", len(rows))
            include(rows)
        with write_phase("display_identity_enumerate", thread_cpu=True):
            while pending:
                batch = [pending.pop() for _ in range(min(500, len(pending)))]
                queried.update(batch)
                rows = one_step_rows(conn,
                    _DISPLAY_IDENTITY_INDEX_SQL + f" AND display_identity IN ({_placeholders(batch)})",
                    (session_id, *batch), ("id", "display_identity", "display_order"))
                add_write_rows("display_identity_rows", len(rows))
                include(rows)
        with write_phase("display_materialize", thread_cpu=True):
            add_write_rows("display_materialized_rows", len(selected))
            return [selected[row_id] for row_id in sorted(selected)], identities
