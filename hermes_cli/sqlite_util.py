"""Shared SQLite primitives for the small per-profile / board stores.

``open_db`` is the one connect + PRAGMA stack; ``transaction`` is the one commit-and-ALWAYS-close
shape. Every hand-rolled ``_connect``/``_transaction`` pair used to re-carry the #69567 fix (a
``with conn:`` only commits — it never closes, so each call leaked a connection and its WAL/SHM fds
until GC, exhausting ``RLIMIT_NOFILE`` on long-running gateways) and at least one copy missed it.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable, Iterator


def open_db(
    path: Path | str,
    *,
    db_label: str,
    busy_timeout_ms: int = 5000,
    wal: bool = True,
    foreign_keys: bool = False,
    synchronous_full: bool = False,
    row_factory=sqlite3.Row,
    check_same_thread: bool = True,
    wal_lock_retries: int = 1,
    initialize: Callable[[sqlite3.Connection], None] | None = None,
    connect_fn: Callable[..., sqlite3.Connection] | None = None,
) -> sqlite3.Connection:
    """Open ``path`` (parent created), apply the PRAGMA set, run ``initialize``; closed if anything raises.

    ``busy_timeout_ms`` is the single busy knob: it is passed as ``connect(timeout=)`` AND set as the
    explicit PRAGMA so it is observable. ``wal=True`` goes through ``apply_wal_with_fallback`` — the
    only journal-mode setter that carries the WAL-reset-bug gate, the network-FS silent-refusal
    fallback and the never-live-downgrade invariant; a raw ``PRAGMA journal_mode=WAL`` bypasses all
    three. Only the transient ``database is locked`` from that pragma is retried (``wal_lock_retries``):
    a first opener initializing a shared DB can make it ignore the busy timeout, notably on Windows.
    """
    from hermes_state_wal import apply_wal_with_fallback

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Resolved at call time: fd-leak tests patch ``sqlite3.connect`` through the caller's module.
    # Shared state.db writers supply its tracked connector to protect live POSIX locks
    # from raw header probes; independent small stores keep the standard connector.
    conn = (connect_fn or sqlite3.connect)(path, timeout=busy_timeout_ms / 1000,
                                          check_same_thread=check_same_thread)
    try:
        conn.row_factory = row_factory
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if wal:
            for attempt in range(wal_lock_retries):
                try:
                    apply_wal_with_fallback(conn, db_label=db_label)
                    break
                except sqlite3.OperationalError as exc:
                    if str(exc).lower() != "database is locked" or attempt + 1 == wal_lock_retries:
                        raise
                    time.sleep(0.01 * (2**attempt))
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")
        if synchronous_full:
            conn.execute("PRAGMA synchronous=FULL")
        if initialize is not None:
            initialize(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def open_shared_state_db(
    path: Path | str,
    *,
    db_label: str,
    initialize: Callable[[sqlite3.Connection], None],
    wal: bool = True,
) -> sqlite3.Connection:
    """Open a raw state.db writer under SessionDB's startup/probe-safety protocol.

    The lock covers empty-file creation through schema commit, not the returned
    connection's lifetime. Only SessionDB recovers invalid stores; other writers
    refuse damaged bytes rather than replacing forensic evidence.
    """
    from hermes_constants import mkdir_under_hermes_home
    from hermes_state import (
        _connect_tracked_db, _secure_state_db_files,
        has_invalid_sqlite_header_preopen, quarantine_cross_process_lock,
    )

    from hermes_state_repair import preflight_db_writability

    path = Path(path)
    mkdir_under_hermes_home(path.parent)
    preflight_db_writability(path, db_label=db_label, directory_only=True)
    with quarantine_cross_process_lock(path) as acquired:
        if not acquired:
            raise sqlite3.OperationalError(f"state.db startup lock not acquired for {path}")
        # Permission repair can turn an indeterminate header probe into an
        # observed invalid one; validate AFTER repair, still under startup lock.
        preflight_db_writability(path, db_label=db_label)
        if has_invalid_sqlite_header_preopen(path):
            raise sqlite3.DatabaseError(
                f"state.db has invalid SQLite header: {path}; open with SessionDB to recover")
        # O_EXCL proves ownership of this inode, not merely that a prior probe
        # found no path. Keep startup failures retryable without deleting a
        # pre-existing/replaced DB or bytes SQLite has already committed.
        sidecars = [Path(str(path) + suffix) for suffix in ("-wal", "-shm")]
        existing_sidecars = {sidecar for sidecar in sidecars if os.path.lexists(sidecar)}
        created = None
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                created = os.fstat(fd)
            finally:
                os.close(fd)
        conn = None
        try:
            _secure_state_db_files(path)
            conn = open_db(path, db_label=db_label, busy_timeout_ms=10_000, wal=wal,
                           row_factory=None, initialize=initialize, connect_fn=_connect_tracked_db)
            conn.commit()
            _secure_state_db_files(path)
            return conn
        except BaseException:
            if conn is not None:
                conn.close()
            if created is not None:
                # All participating openers still wait on the startup lock.
                # Do not let cleanup errors hide the original open failure.
                with contextlib.suppress(OSError):
                    current = path.lstat()
                    if ((current.st_dev, current.st_ino) == (created.st_dev, created.st_ino)
                            and current.st_size == 0):
                        for sidecar in sidecars:
                            if sidecar not in existing_sidecars:
                                with contextlib.suppress(OSError):
                                    if sidecar.lstat().st_size == 0:
                                        sidecar.unlink()
                        path.unlink()
            raise


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, and ALWAYS close ``conn`` (see the module docstring)."""
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        with conn:
            yield conn
    finally:
        conn.close()


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """``ALTER TABLE <table> ADD COLUMN <ddl>``, idempotent across races: True when this call added
    it, False on the ``duplicate column name`` a concurrent migrator caused.

    ``column`` is the human-readable name for the call site; ``ddl`` carries the actual definition. See
    #21708.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return False
        raise


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """An IMMEDIATE write transaction on a long-lived connection (stays open). The explicit ROLLBACK is
    guarded so a SQLite auto-rollback (no transaction left under EIO / contention / corruption) cannot
    shadow the original error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        conn.execute("COMMIT")
