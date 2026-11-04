"""The delegation ledger shares state.db's startup/quarantine boundary."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest


@pytest.mark.parametrize("module", ["tools.async_delegation", "gateway.delivery_ledger"])
def test_ledger_first_open_is_not_quarantined_by_sessiondb(tmp_path, monkeypatch, module):
    """Pause a real ledger connection in the empty-file -> schema-commit window.

    A second SessionDB must wait, not move the ledger's live inode and sidecars.
    This is the competing opener in the session-end CI flake; [process] there
    describes a notification type, not a subprocess.
    """
    import contextlib
    import importlib
    import hermes_state as hs
    from hermes_cli.sqlite_safe_read import _TrackingMixin

    ledger = importlib.import_module(module)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "state.db"
    monkeypatch.setattr(ledger, "_db_path", lambda: path)
    connected, release, peer_started, peer_finished = (threading.Event() for _ in range(4))
    connect, tracked_connect = sqlite3.connect, hs._connect_tracked_db
    startup_lock = hs.quarantine_cross_process_lock

    def pause(conn):
        assert path.stat().st_size == 0
        connected.set()
        assert release.wait(10), "ledger initializer was not released"
        return conn

    def paused_connect(database, *args, **kwargs):
        conn = connect(database, *args, **kwargs)
        # Keep the old untracked opener reproducible too, without ever pausing
        # inside connect_tracked's global probe-safety lock.
        if str(database) == str(path) and not isinstance(conn, _TrackingMixin) and not connected.is_set():
            pause(conn)
        return conn

    def paused_tracked_connect(database, *args, **kwargs):
        conn = tracked_connect(database, *args, **kwargs)
        if str(database) == str(path) and not connected.is_set():
            pause(conn)
        return conn

    @contextlib.contextmanager
    def observed_lock(database, *args, **kwargs):
        if connected.is_set():
            peer_started.set()
        with startup_lock(database, *args, **kwargs) as acquired:
            yield acquired

    monkeypatch.setattr(sqlite3, "connect", paused_connect)
    monkeypatch.setattr(hs, "_connect_tracked_db", paused_tracked_connect)
    monkeypatch.setattr(hs, "quarantine_cross_process_lock", observed_lock)

    def open_ledger():
        conn = ledger._connect()
        try:
            conn.execute("CREATE TABLE startup_marker (owner TEXT)")
            conn.execute("INSERT INTO startup_marker VALUES ('ledger-owner')")
            conn.commit()
        finally:
            conn.close()

    def open_session():
        try:
            db = hs.SessionDB(db_path=path)
            try:
                db.create_session("session-owner", source="test")
            finally:
                db.close()
        finally:
            peer_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(open_ledger)
        try:
            assert connected.wait(10)
            second = pool.submit(open_session)
            assert peer_started.wait(10)
            # Under the old code the peer finishes by quarantining the live
            # empty DB. The first opener must own startup until schema commit.
            assert not peer_finished.wait(0.25), "SessionDB bypassed ledger startup"
        finally:
            release.set()
        first.result(timeout=10)
        second.result(timeout=10)

    assert not list(tmp_path.glob("state.db.*.bak")), "a live database was quarantined"
    db = hs.SessionDB(db_path=path)
    try:
        assert db._conn.execute("SELECT owner FROM startup_marker").fetchone()[0] == "ledger-owner"
        assert db.get_session("session-owner")
        assert db._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()


@pytest.mark.parametrize("module", ["tools.async_delegation", "gateway.delivery_ledger"])
def test_ledger_connection_is_visible_to_byte_probe_guard(tmp_path, monkeypatch, module):
    import importlib
    from hermes_cli.sqlite_safe_read import has_live_connection

    ledger = importlib.import_module(module)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")
    conn = ledger._connect()
    try:
        assert has_live_connection(tmp_path / "state.db")
    finally:
        conn.close()
    assert not has_live_connection(tmp_path / "state.db")


@pytest.mark.parametrize("module", ["tools.async_delegation", "gateway.delivery_ledger"])
@pytest.mark.parametrize("damage", [b"", b"not a SQLite header" * 100], ids=["empty", "clobbered"])
def test_ledger_refuses_invalid_existing_db_without_moving_evidence(tmp_path, monkeypatch, damage, module):
    import importlib

    ledger = importlib.import_module(module)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "state.db"
    monkeypatch.setattr(ledger, "_db_path", lambda: path)
    path.write_bytes(damage)
    with pytest.raises(sqlite3.DatabaseError, match="invalid SQLite header"):
        ledger._connect()
    assert path.read_bytes() == damage
    assert not list(tmp_path.glob("state.db.*.bak"))

@pytest.mark.parametrize("module", ["tools.async_delegation", "gateway.delivery_ledger"])
def test_ledger_startup_lock_failure_leaves_no_database(tmp_path, monkeypatch, module):
    import contextlib
    import importlib
    import hermes_state as hs

    ledger = importlib.import_module(module)
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    @contextlib.contextmanager
    def unavailable(_path):
        yield False

    monkeypatch.setattr(hs, "quarantine_cross_process_lock", unavailable)
    with pytest.raises(sqlite3.OperationalError, match="startup lock not acquired"):
        ledger._connect()
    assert not (tmp_path / "state.db").exists()


def test_ledger_rechecks_invalid_header_after_permission_repair(tmp_path, monkeypatch):
    import os
    from tools import async_delegation as ledger

    if os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("requires POSIX non-root permission checks")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "state.db"
    path.write_bytes(b"")
    path.chmod(0)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="invalid SQLite header"):
            ledger._connect()
        assert path.read_bytes() == b""
        assert not list(tmp_path.glob("state.db.*.bak"))
    finally:
        path.chmod(0o600)

def _process_ledger_first_open(home, connected, release):
    import os
    from pathlib import Path
    from tools import async_delegation as ledger

    os.environ["HERMES_HOME"] = home
    original = ledger._initialize_schema

    def paused(conn):
        assert (Path(home) / "state.db").stat().st_size == 0
        connected.set()
        assert release.wait(20)
        original(conn)

    ledger._initialize_schema = paused
    conn = ledger._connect()
    try:
        conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('process-owner', 'test', 1)")
        conn.commit()
    finally:
        conn.close()


def test_separate_process_ledger_startup_is_not_quarantined(tmp_path, monkeypatch):
    import contextlib
    import multiprocessing
    import hermes_state as hs

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    attempted = threading.Event()
    startup_lock = hs.quarantine_cross_process_lock

    @contextlib.contextmanager
    def observed_lock(path, *args, **kwargs):
        attempted.set()
        with startup_lock(path, *args, **kwargs) as acquired:
            yield acquired

    monkeypatch.setattr(hs, "quarantine_cross_process_lock", observed_lock)
    context = multiprocessing.get_context("spawn")
    connected, release = context.Event(), context.Event()
    child = context.Process(target=_process_ledger_first_open, args=(str(tmp_path), connected, release))
    child.start()
    try:
        assert connected.wait(20), "child never reached the live empty-file window"
        with ThreadPoolExecutor(max_workers=1) as pool:
            peer = pool.submit(hs.SessionDB, db_path=tmp_path / "state.db")
            try:
                assert attempted.wait(10), "peer never attempted the startup lock"
                with pytest.raises(TimeoutError):
                    peer.result(timeout=0.25)
            finally:
                release.set()
            db = peer.result(timeout=20)
            try:
                db.create_session("session-owner", source="test")
            finally:
                db.close()
        child.join(timeout=20)
        assert child.exitcode == 0
        assert not list(tmp_path.glob("state.db.*.bak"))
        db = hs.SessionDB(db_path=tmp_path / "state.db")
        try:
            assert db.get_session("process-owner")
            assert db.get_session("session-owner")
            assert db._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            db.close()
    finally:
        release.set()
        child.join(timeout=5)
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
