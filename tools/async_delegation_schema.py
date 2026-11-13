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
    # full DDL each time held that lock for the whole reconcile. Once per database file is enough;
    # the inode key re-reconciles a state.db that was replaced or restored mid-process.
    key = _schema_key(conn)
    if key is not None and key in _SCHEMA_RECONCILED:
        return
    reconcile_state_schema(conn)
    if key is not None:
        _SCHEMA_RECONCILED.add(key)


_SCHEMA_RECONCILED: set[tuple[str, int]] = set()


def _schema_key(conn: sqlite3.Connection) -> Optional[tuple[str, int]]:
    try:
        path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
        return (path, os.stat(path).st_ino) if path else None
    except (sqlite3.Error, OSError, StopIteration):
        return None
