"""WAL-safe SQLite snapshots. Direct execution needs only the standard library.

Desktop invokes this file before stopping its backend, even when application
imports cannot load. Full and quick backups use the same SQLite copy operation.
"""
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class _SQLiteBackupTimeout(RuntimeError):
    """Raised when a SQLite snapshot remains busy past its deadline."""


_SNAPSHOT_FREE_SPACE_MARGIN = 64 * 1024 * 1024


def _required_snapshot_bytes(src: Path) -> int:
    """Estimate the space needed before opening a snapshot destination.

    SQLite's backup API materializes the database image at the destination and
    may also need the live WAL while the source is being read.  Refuse to start
    a large copy when the destination volume cannot hold both, rather than
    discovering ENOSPC after hours of copying.
    """
    required = src.stat().st_size
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{src}{suffix}")
        with suppress(OSError):
            required += sidecar.stat().st_size
    return required + _SNAPSHOT_FREE_SPACE_MARGIN


def _close_quietly(conn: Optional[sqlite3.Connection]) -> None:
    if conn is not None:
        with suppress(Exception):
            conn.close()


def _safe_copy_db(src: Path, dst: Path, *, timeout_seconds: float = 10.0) -> bool:
    """Copy a SQLite database with the backup() API (WAL-safe consistent snapshot).

    Fails closed when no consistent snapshot can be made: copying only the main file loses WAL data.
    """
    conn = backup_conn = None
    try:
        required_bytes = _required_snapshot_bytes(src)
        available_bytes = shutil.disk_usage(dst.parent).free
        if available_bytes < required_bytes:
            raise OSError(
                f"insufficient free space for SQLite snapshot: "
                f"need {required_bytes} bytes, have {available_bytes}"
            )
        # sqlite3.connect() creates a missing destination with the process
        # umask, which is commonly 0022 (0644).  Snapshot databases contain
        # session and tool state, so create the inode owner-only before SQLite
        # writes its first byte.  O_NOFOLLOW also refuses a planted symlink on
        # platforms that support it.  Tighten an existing internal staging
        # file as well (NamedTemporaryFile callers already create it 0600).
        if os.name != "nt":
            open_flags = os.O_WRONLY | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                open_flags |= os.O_NOFOLLOW
            secure_fd = os.open(dst, open_flags, 0o600)
            try:
                os.fchmod(secure_fd, 0o600)
            finally:
                os.close(secure_fd)
        # timeout=0.0 disables sqlite3's implicit busy wait so the progress callback owns the
        # full locked-source deadline instead of adding the default timeout before each callback.
        conn = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=0.0)
        backup_conn = sqlite3.connect(str(dst))
        timeout_seconds = max(0.0, timeout_seconds)
        setup_deadline = time.monotonic() + timeout_seconds

        def _snapshot_setup(query: str, *, fetchone: bool = False):
            """Run snapshot initialization without restoring sqlite3's per-call busy timeout."""
            while True:
                try:
                    cursor = conn.execute(query)
                    return cursor.fetchone() if fetchone else cursor
                except sqlite3.OperationalError as exc:
                    error_code = getattr(exc, "sqlite_errorcode", None)
                    primary_code = error_code & 0xFF if isinstance(error_code, int) else None
                    if primary_code not in (
                        sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                    ):
                        raise
                    remaining = setup_deadline - time.monotonic()
                    if remaining <= 0:
                        raise _SQLiteBackupTimeout(
                            f"database remained locked for {timeout_seconds:g} seconds"
                        ) from exc
                    time.sleep(min(0.1, remaining))

        journal_mode = _snapshot_setup("PRAGMA journal_mode", fetchone=True)
        if journal_mode and str(journal_mode[0]).lower() == "wal":
            # Incremental backup releases its per-step read lock, so an external WAL writer
            # otherwise restarts the copy from page zero on every commit. Pin one WAL snapshot
            # for the whole copy: WAL writers may continue, while rollback-journal databases
            # retain the existing short per-step locks instead of blocking writers for minutes.
            _snapshot_setup("BEGIN")
            _snapshot_setup("SELECT 1 FROM sqlite_schema LIMIT 1", fetchone=True)
        busy_deadline = time.monotonic() + timeout_seconds

        def _check_backup_progress(status: int, _remaining: int, _total: int) -> None:
            nonlocal busy_deadline
            now = time.monotonic()
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                if now >= busy_deadline:
                    raise _SQLiteBackupTimeout(f"database remained locked for {timeout_seconds:g} seconds")
            else:
                busy_deadline = now + timeout_seconds

        conn.backup(backup_conn, pages=256, progress=_check_backup_progress, sleep=0.1)
        return True
    except Exception as exc:
        logger.warning("SQLite safe copy failed for %s: %s", src, exc)
        # Windows won't remove the partial destination while SQLite still has it open.
        _close_quietly(backup_conn)
        backup_conn = None
        with suppress(OSError):
            dst.unlink(missing_ok=True)
        return False
    finally:
        _close_quietly(backup_conn)
        _close_quietly(conn)


def preflight_state_db(home: Path) -> dict:
    """Publish an emergency snapshot; do not prune recovery files on failure."""
    source = home / "state.db"
    if not source.exists():
        return {"path": None, "message": "state.db not found (fresh install?)"}
    prefix = "state.db.pre-update-emergency-"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    destination = home / f"{prefix}{stamp}-{os.getpid()}.bak"
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".partial", dir=home)
    os.close(fd)
    staged = Path(name)
    try:
        if not _safe_copy_db(source, staged):
            raise RuntimeError("SQLite safe copy failed; previous emergency snapshots were retained")
        connection = sqlite3.connect(str(staged))
        try:
            result = connection.execute("PRAGMA quick_check").fetchall()
            if result != [("ok",)]:
                raise RuntimeError(f"SQLite snapshot integrity check failed: {result}")
        finally:
            connection.close()
        size = staged.stat().st_size
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    for old in sorted(home.glob(f"{prefix}*.bak"), reverse=True)[2:]:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("Could not prune emergency snapshot %s: %s", old, exc)
    return {"path": str(destination), "bytes": size}


if __name__ == "__main__":
    print(json.dumps(preflight_state_db(Path(sys.argv[1]))))
