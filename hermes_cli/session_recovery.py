"""Offline, non-destructive recovery for a damaged Hermes session database.

The store admin's ``recover_transcripts`` runs it: the source is never opened by SQLite (it and its WAL/SHM/journal
sidecars are copied to a disposable work dir first), canonical rows are copied into a fresh current-schema database,
derived FTS tables and migration bookkeeping are rebuilt, not copied, and the result is never installed over the
active database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from persistence.core.admin import SessionRecoveryError, SessionRecoverySafetyError, SessionRecoverySourceError
from persistence_assembly import open_store_admin

__all__ = [
    "SessionRecoveryError", "SessionRecoverySafetyError", "SessionRecoverySourceError", "inspect_session_database",
    "recover_session_database", "write_recovery_report",
]

ProgressCallback = Callable[[dict[str, Any]], None]
<<<<<<< HEAD
_CANONICAL_TABLES = (
    "system_prompts", "sessions", "messages", "session_model_usage", "compression_locks", "gateway_routing",
    "async_delegations",
)
_TOPIC_TABLES = ("telegram_dm_topic_mode", "telegram_dm_topic_bindings")


def _init_delivery_ledger_schema(conn: sqlite3.Connection) -> None:
    from gateway.delivery_ledger import _initialize_schema
    _initialize_schema(conn)


def _init_async_delegation_events_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS async_delegation_events (
            event_id TEXT PRIMARY KEY,
            delegation_id TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            event_json TEXT NOT NULL,
            result_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            delivery_claim TEXT,
            delivery_claimed_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_async_delegation_events_delivery
            ON async_delegation_events(delivery_state, updated_at);
        """
    )


# state.db tables created lazily by a gateway module (base ``SessionDB`` never creates them on a fresh
# destination) -> the initializer owning their DDL. Recovery creates them before copying so owed rows
# don't silently vanish from a "complete" salvage. Register new lazy tables HERE, not as ``if table ==``.
# See #100313, #86236.
_AUXILIARY_TABLE_SCHEMAS: dict[str, Callable[[sqlite3.Connection], None]] = {
    "delivery_obligations": _init_delivery_ledger_schema,
    "async_delegation_events": _init_async_delegation_events_schema,
}
_AUXILIARY_TABLES = tuple(_AUXILIARY_TABLE_SCHEMAS)
# Salvage is a transcript output, not a runnable clone of accepted work. Copying
# only part of this ledger could replay already-started effects or adopt workers
# from another owner. Inventory and report the exclusions rather than losing them
# silently. Activation under exclusive ownership creates a fresh runtime epoch;
# ordinary full backup/restore instead preserves the complete ledger for startup
# reconciliation (started -> unknown; never blindly replay started work).
_RUNTIME_TABLES = ("runtime_epoch", "session_admissions", "worker_executions", "worker_receipts")
_INVENTORY_TABLES = (*_CANONICAL_TABLES, "state_meta", *_TOPIC_TABLES, *_AUXILIARY_TABLES, *_RUNTIME_TABLES)

# Derived-index / optional-schema markers: a fresh destination regenerates these, never copies them.
_GENERATED_META_KEYS = frozenset({
    "fts_storage_version", "fts_optimize_available", "fts_rebuild_high_water", "fts_rebuild_progress",
    "fts_cjk_stale", "fts_cjk_rebuild_high_water", "fts_cjk_rebuild_progress", "telegram_dm_topic_schema_version",
    "fts_tool_full_content_high_water",  # retired marker; never copied into a recovered store
})
_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")
_MINIMUM_SPACE_HEADROOM = 256 * 1024 * 1024
_MAX_SALVAGE_RANGE_QUERIES = 10_000
_MIN_SQLITE_ROWID = -(2**63)
_MAX_SQLITE_ROWID = 2**63 - 1


class SessionRecoveryError(RuntimeError):
    """Base error for offline session recovery."""


class SessionRecoverySafetyError(SessionRecoveryError):
    """Raised before recovery when a path or overwrite guard fails."""


class SessionRecoverySourceError(SessionRecoveryError):
    """Raised when the source cannot provide the required canonical tables."""


def _sidecar_path(db_path: Path, suffix: str) -> Path:
    return db_path if not suffix else db_path.with_name(db_path.name + suffix)
||||||| dff103e05878
_CANONICAL_TABLES = (
    "system_prompts", "sessions", "messages", "session_model_usage", "compression_locks", "gateway_routing",
    "async_delegations",
)
_TOPIC_TABLES = ("telegram_dm_topic_mode", "telegram_dm_topic_bindings")


def _init_delivery_ledger_schema(conn: sqlite3.Connection) -> None:
    from gateway.delivery_ledger import _initialize_schema
    _initialize_schema(conn)


# state.db tables created lazily by a gateway module (base ``SessionDB`` never creates them on a fresh
# destination) -> the initializer owning their DDL. Recovery creates them before copying so owed rows
# don't silently vanish from a "complete" salvage. Register new lazy tables HERE, not as ``if table ==``.
# See #100313, #86236.
_AUXILIARY_TABLE_SCHEMAS: dict[str, Callable[[sqlite3.Connection], None]] = {
    "delivery_obligations": _init_delivery_ledger_schema,
}
_AUXILIARY_TABLES = tuple(_AUXILIARY_TABLE_SCHEMAS)
# Salvage is a transcript output, not a runnable clone of accepted work. Copying
# only part of this ledger could replay already-started effects or adopt workers
# from another owner. Inventory and report the exclusions rather than losing them
# silently. Activation under exclusive ownership creates a fresh runtime epoch;
# ordinary full backup/restore instead preserves the complete ledger for startup
# reconciliation (started -> unknown; never blindly replay started work).
_RUNTIME_TABLES = ("runtime_epoch", "session_admissions", "worker_executions", "worker_receipts")
_INVENTORY_TABLES = (*_CANONICAL_TABLES, "state_meta", *_TOPIC_TABLES, *_AUXILIARY_TABLES, *_RUNTIME_TABLES)

# Derived-index / optional-schema markers: a fresh destination regenerates these, never copies them.
_GENERATED_META_KEYS = frozenset({
    "fts_storage_version", "fts_optimize_available", "fts_rebuild_high_water", "fts_rebuild_progress",
    "fts_cjk_stale", "fts_cjk_rebuild_high_water", "fts_cjk_rebuild_progress", "telegram_dm_topic_schema_version",
    "fts_tool_full_content_high_water",  # retired marker; never copied into a recovered store
})
_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")
_MINIMUM_SPACE_HEADROOM = 256 * 1024 * 1024
_MAX_SALVAGE_RANGE_QUERIES = 10_000
_MIN_SQLITE_ROWID = -(2**63)
_MAX_SQLITE_ROWID = 2**63 - 1


class SessionRecoveryError(RuntimeError):
    """Base error for offline session recovery."""


class SessionRecoverySafetyError(SessionRecoveryError):
    """Raised before recovery when a path or overwrite guard fails."""


class SessionRecoverySourceError(SessionRecoveryError):
    """Raised when the source cannot provide the required canonical tables."""


def _sidecar_path(db_path: Path, suffix: str) -> Path:
    return db_path if not suffix else db_path.with_name(db_path.name + suffix)
=======
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca


def _resolved_output_path(path: Path) -> Path:
    """Resolve a not-yet-created output path without requiring it to exist."""
    return path.expanduser().parent.resolve(strict=True) / path.name


def inspect_session_database(source_path: Path, *, work_dir: Optional[Path] = None) -> dict[str, Any]:
    """Inspect canonical table readability without opening the source itself."""
    # An inspection writes nothing, so the output argument goes unused.
    return open_store_admin(source_path).recover_transcripts(source_path, source_path, inspect_only=True,
                                                             work_dir=work_dir)


def recover_session_database(
    source_path: Path, output_path: Path, *, work_dir: Optional[Path] = None, chunk_size: int = 1_000,
    progress_cb: Optional[ProgressCallback] = None, allow_partial: bool = False,
) -> dict[str, Any]:
    """Separate-output salvage is allowed; a canonical output requires offline ownership."""
    return open_store_admin(source_path).recover_transcripts(
        source_path, output_path, work_dir=work_dir, chunk_size=chunk_size, progress_cb=progress_cb,
        allow_partial=allow_partial,
    )


def write_recovery_report(path: Path, report: dict[str, Any]) -> Path:
    """Write a JSON report without overwriting an existing file."""
    destination = _resolved_output_path(path)
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return destination
