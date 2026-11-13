"""Schema setup for the async-delegation ledger connections (split from async_delegation)."""
from __future__ import annotations

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
    # schema change and a replaced or recreated file starts from its own value, so an unchanged
    # version for this path means the canonical DDL already applied.
    path, version = _schema_state(conn)
    if path is not None and _SCHEMA_RECONCILED.get(path) == version:
        return
    reconcile_state_schema(conn)
    if path is not None:
        _SCHEMA_RECONCILED[path] = _schema_state(conn)[1]


_SCHEMA_RECONCILED: dict[str, int] = {}


def _schema_state(conn: sqlite3.Connection) -> tuple[Optional[str], Optional[int]]:
    try:
        path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
        return (path or None), conn.execute("PRAGMA schema_version").fetchone()[0]
    except (sqlite3.Error, StopIteration):
        return None, None
