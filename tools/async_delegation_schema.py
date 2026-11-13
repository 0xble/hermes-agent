"""Schema setup for the async-delegation ledger connections (split from async_delegation)."""
from __future__ import annotations

import os
import sqlite3
from typing import Optional


def initialize_ledger_schema(conn: sqlite3.Connection) -> None:
    from hermes_state_repair import apply_durability_barriers
    from hermes_state_schema import reconcile_state_schema
    # Preserve the journal mode SessionDB configured on state.db: forcing WAL from
    # every short-lived connection collides with live transcript/FTS writers.
    apply_durability_barriers(conn)
    # Single durable-shape authority: the canonical SCHEMA_SQL drives both
    # table creation and column backfill (reconcile_state_schema replays the
    # canonical DDL and reuses SessionDB's declarative reconciliation). This
    # module previously carried its own CREATE TABLE + ALTER column list,
    # which drifted from SCHEMA_SQL — same-name columns with different
    # nullability/defaults depending on which authority touched the database
    # first (#94691).
    # Every ledger transaction opens a fresh connection, often under _records_lock, so replaying the
    # full DDL each time held that lock for the whole reconcile. SQLite bumps schema_version on any
    # schema change, but a restored file can reuse both its inode and its schema cookie. Include the
    # ledger's stored table definition as a cheap content signal that changes with its columns, not
    # with ordinary row writes. No second list of required columns to keep in sync with SCHEMA_SQL.
    path, state = _schema_state(conn)
    if path is not None and _SCHEMA_RECONCILED.get(path) == state:
        return
    reconcile_state_schema(conn)
    path, state = _schema_state(conn)
    if path is not None and state is not None:
        _SCHEMA_RECONCILED[path] = state


_SCHEMA_RECONCILED: dict[str, tuple[int, int, int, Optional[str]]] = {}


def _schema_state(
    conn: sqlite3.Connection,
) -> tuple[Optional[str], Optional[tuple[int, int, int, Optional[str]]]]:
    try:
        path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
        if not path:
            return None, None
        stat = os.stat(path)
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'async_delegations'"
        ).fetchone()
        return path, (stat.st_dev, stat.st_ino, version, table[0] if table else None)
    except (OSError, sqlite3.Error, StopIteration):
        return None, None
