"""The raw-copy quarantine/backup paths hold the connection-lifecycle lock.

``offline_file_access`` (hermes_cli.sqlite_safe_read) exists because checking
``has_live_connection()`` and *then* doing raw file I/O is a check/use race: a
connection opened in the window between the two has its POSIX advisory locks
cancelled by the raw ``close()`` -- the exact corruption route the registry
guards against. The snapshot path in ``session_recovery`` was converted to the
context manager when it landed; these tests pin the other two raw-copy sites
to the same contract:

* ``hermes_state._backup_db_file`` (malformed-DB backup before schema surgery)
* ``hermes_cli.kanban_db._backup_corrupt_db`` (corrupt-board quarantine)

Each site gets the same pair: a live connection means refusal, and a
connection attempted MID-COPY blocks on the lifecycle lock until the copy is
done rather than slipping into the gap.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading

import pytest

import hermes_state
from hermes_cli import kanban_db
from hermes_cli import sqlite_safe_read
from hermes_cli.sqlite_safe_read import connect_tracked


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts and ends with an empty live-connection registry.

    The registry is process-global; a connection leaked by one test would make
    ``offline_file_access`` in the next raise ``LiveConnectionError`` and
    silently skip the copy under test.
    """
    with sqlite_safe_read._live_lock:
        sqlite_safe_read._live_connections.clear()
    yield
    with sqlite_safe_read._live_lock:
        sqlite_safe_read._live_connections.clear()


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    (tmp_path / "state.db-wal").write_bytes(b"wal")
    return path


def _connect_attempt_during(monkeypatch, db_path):
    """Patch ``shutil.copy2`` so a tracked connect races the first copy.

    The verdict is taken INSIDE the patched copy, while the raw I/O is still
    in flight -- once the site under test returns, its guard has released the
    lifecycle lock and the queued connect is free to land, so a check after
    the return would race the very thing it measures.

    Returns (thread, holder, verdict): ``verdict["landed_during_copy"]`` says
    whether the connect got through mid-copy; ``holder`` collects the
    connection so the test can close it.
    """
    real_copy2 = shutil.copy2
    started = threading.Event()
    holder: list[sqlite3.Connection] = []
    verdict: dict = {"landed_during_copy": None}

    def _racing_connect():
        started.set()
        conn = connect_tracked(str(db_path), check_same_thread=False)
        holder.append(conn)

    thread = threading.Thread(target=_racing_connect, daemon=True)
    fired = threading.Event()

    def _copy2_with_race(src, dst, *a, **kw):
        if not fired.is_set():
            fired.set()
            thread.start()
            started.wait(timeout=5)
            # Give the connector every chance to slip in if nothing blocks it.
            thread.join(timeout=0.5)
            verdict["landed_during_copy"] = bool(holder)
        return real_copy2(src, dst, *a, **kw)

    # Both call sites bind the stdlib module object (kanban_db at module
    # level, hermes_state via a function-local ``import shutil``), so one
    # patch on the module covers both.
    monkeypatch.setattr(shutil, "copy2", _copy2_with_race)
    return thread, holder, verdict


# ---------------------------------------------------------------------------
# hermes_state._backup_db_file
# ---------------------------------------------------------------------------

def test_backup_db_file_refuses_with_live_connection(db_file):
    conn = connect_tracked(str(db_file))
    try:
        assert hermes_state._backup_db_file(db_file) is None
        assert not list(db_file.parent.glob("*.malformed-backup-*"))
    finally:
        conn.close()


def test_backup_db_file_copy_is_atomic_with_the_registry(db_file, monkeypatch):
    """A connect attempted mid-copy must wait, not land in the gap."""
    thread, holder, verdict = _connect_attempt_during(monkeypatch, db_file)

    result = hermes_state._backup_db_file(db_file)

    assert result is not None and result.exists()
    assert verdict["landed_during_copy"] is False, (
        "a connection was opened while the raw copy was in flight -- its "
        "POSIX locks would be cancelled by the copy's close()"
    )
    thread.join(timeout=5)
    assert holder, "the queued connect must succeed once the copy is done"
    holder[0].close()


def test_backup_db_file_still_copies_without_the_registry(db_file, monkeypatch):
    """Constrained embeds without hermes_cli keep the best-effort copy."""
    import builtins

    real_import = builtins.__import__

    def _no_safe_read(name, *args, **kwargs):
        if name == "hermes_cli.sqlite_safe_read":
            raise ImportError("embed path")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_safe_read)
    result = hermes_state._backup_db_file(db_file)
    assert result is not None and result.exists()
    assert result.with_name(result.name + "-wal").exists()


# ---------------------------------------------------------------------------
# hermes_cli.kanban_db._backup_corrupt_db
# ---------------------------------------------------------------------------

def test_backup_corrupt_db_refuses_with_live_connection(db_file):
    conn = connect_tracked(str(db_file))
    try:
        assert kanban_db._backup_corrupt_db(db_file) is None
        assert not list(db_file.parent.glob("*.corrupt.*.bak"))
    finally:
        conn.close()


def test_backup_corrupt_db_copy_is_atomic_with_the_registry(db_file, monkeypatch):
    thread, holder, verdict = _connect_attempt_during(monkeypatch, db_file)

    result = kanban_db._backup_corrupt_db(db_file)

    assert result is not None and result.exists()
    assert verdict["landed_during_copy"] is False, (
        "a connection was opened while the quarantine fingerprint/copy was "
        "in flight -- its POSIX locks would be cancelled by our close()"
    )
    thread.join(timeout=5)
    assert holder, "the queued connect must succeed once the quarantine is done"
    holder[0].close()


def test_backup_corrupt_db_still_copies_sidecars(db_file):
    result = kanban_db._backup_corrupt_db(db_file)
    assert result is not None and result.exists()
    assert result.with_name(result.name + "-wal").exists()
