"""Migrate authoritative topic routing without copying or rewriting transcript history."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time


def plan_routing_migration(home: Path, aliases: dict, changes: list[str]) -> list[tuple]:
    """Read an existing selected database without bootstrapping it or changing its schema."""
    from hermes_cli.config_migrations_model_aliases import rewrite_references
    path = home / "state.db"
    if not path.is_file():
        return []
    pending = []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gateway_routing'").fetchone() is None:
            return []
        for scope, key, original in conn.execute("SELECT scope, session_key, entry_json FROM gateway_routing"):
            raw = json.loads(original)
            updated = rewrite_references(raw, aliases, changes, f"state.db.gateway_routing[{scope!r}, {key!r}]")
            if updated != raw:
                pending.append((scope, key, original, json.dumps(updated)))
    return pending


def apply_routing_migration(home: Path, pending: list[tuple]) -> None:
    """Use the native writer guard and one transaction, refusing a concurrent route edit."""
    if not pending:
        return
    from hermes_state import SessionDB
    db = SessionDB(home / "state.db")
    try:
        def apply(conn):
            for scope, key, original, updated in pending:
                cursor = conn.execute(
                    "UPDATE gateway_routing SET entry_json = ?, updated_at = ? "
                    "WHERE scope = ? AND session_key = ? AND entry_json = ?",
                    (updated, time.time(), scope, key, original),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Topic route {key!r} changed during migration; retry from its current state")
        db._execute_write(apply)
    finally:
        db.close()
