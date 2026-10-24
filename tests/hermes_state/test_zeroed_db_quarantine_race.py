"""A concurrent opener's writability preflight must not race another opener's quarantine.

Two processes (gateway + CLI) starting on a zeroed state.db: the preflight lists ``state.db``
with ``is_file()``, the other opener's quarantine renames it aside, and ``os.access`` then
fails with ENOENT. That used to be reported as "state.db is not writable ... read-only for
this user" (flaky ``test_concurrent_quarantine_no_clobber`` on CI). Event-driven, no sleeps.
"""

import os
import sqlite3
import threading
from pathlib import Path

import pytest

import hermes_state as hs
import hermes_state_repair
import hermes_state_dbfile as dbfile
import hermes_state_repair as repair


def test_preflight_cannot_observe_db_mid_quarantine(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    db.write_bytes(bytes(4096))  # zeroed 4 KB file

    late_reached = threading.Event()   # worker 1 hit its preflight check OR the quarantine lock
    quarantined = threading.Event()    # worker 0 finished moving the zeroed file aside
    real_access = os.access
    real_lock = hs.quarantine_cross_process_lock
    real_quarantine = hs.quarantine_invalid_state_db
    errors: list = [None, None]

    def is_late() -> bool:
        return threading.current_thread().name == "late-opener"

    def gated_access(path, mode, *args, **kwargs):
        # Worker 1 has listed state.db via is_file(); hold its access check until worker 0's
        # quarantine has renamed the file (the CI interleaving, forced).
        if is_late() and Path(path) == db and not quarantined.is_set():
            late_reached.set()
            assert quarantined.wait(10), "worker 0 never quarantined"
        return real_access(path, mode, *args, **kwargs)

    def signalling_lock(path, *args, **kwargs):
        if is_late():
            late_reached.set()  # about to block on the lock worker 0 holds
        return real_lock(path, *args, **kwargs)

    def worker(idx):
        try:
            hs.SessionDB(db_path=db).close()
        except Exception as exc:  # surfaced by the assertions below
            errors[idx] = exc

    late = threading.Thread(target=worker, args=(1,), name="late-opener")

    def quarantine_then_release(path, **kwargs):
        # Worker 0 holds the quarantine lock here; start worker 1 against the still-zeroed file.
        late.start()
        assert late_reached.wait(10), "worker 1 never reached its preflight or the lock"
        try:
            return real_quarantine(path, **kwargs)
        finally:
            quarantined.set()

    monkeypatch.setattr(hermes_state_repair.os, "access", gated_access)
    monkeypatch.setattr(hs, "quarantine_cross_process_lock", signalling_lock)
    monkeypatch.setattr(hs, "quarantine_invalid_state_db", quarantine_then_release)

    first = threading.Thread(target=worker, args=(0,), name="first-opener")
    first.start()
    first.join(20)
    late.join(20)
    assert not first.is_alive() and not late.is_alive()

    assert errors == [None, None], f"concurrent open raised: {errors}"
    baks = list(tmp_path.glob("state.db.zeroed-*.bak"))
    assert len(baks) == 1 and baks[0].read_bytes() == bytes(4096)


# Fork extension: the absent-then-recreated (ABA) interleaving and unreadable-header
# permission repair, adapted from the upstream test above.


def test_preflight_settles_absent_then_recreated_replacement(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = tmp_path / "state.db"  # Outside the chmod/re-access repair scope.
    db.write_bytes(bytes(4096))

    late_reached = threading.Event()
    access_reached = threading.Event()
    quarantined = threading.Event()
    missing_observed = threading.Event()
    recreated = threading.Event()
    real_access = os.access
    real_lock = hs.quarantine_cross_process_lock
    real_quarantine = hs.quarantine_invalid_state_db
    real_connect = hs.SessionDB._connect_and_init_with_lock_patience
    errors: list[BaseException | None] = [None, None]
    observations = []

    def is_late():
        return threading.current_thread().name == "late-opener"

    def gated_access(path, mode, *args, **kwargs):
        if is_late() and Path(path) == db and not quarantined.is_set():
            access_reached.set()
            late_reached.set()
            assert quarantined.wait(10), "first opener never quarantined"
            result = real_access(path, mode, *args, **kwargs)
            observations.append((result, db.exists()))
            missing_observed.set()
            assert recreated.wait(10), "first opener never recreated the DB"
            assert real_access(path, mode, *args, **kwargs)
            return result
        return real_access(path, mode, *args, **kwargs)

    def signalling_lock(path, *args, **kwargs):
        if is_late():
            late_reached.set()
        return real_lock(path, *args, **kwargs)

    def worker(idx):
        try:
            sdb = hs.SessionDB(db_path=db)
            try:
                sdb.create_session(session_id=f"opener-{idx}", source="test")
            finally:
                sdb.close()
        except BaseException as exc:
            errors[idx] = exc

    late = threading.Thread(target=worker, args=(1,), name="late-opener")

    def quarantine_then_release(path, **kwargs):
        # First opener owns the existing lock. On base the late opener reaches
        # unlocked access, on the repair it reaches the lock and blocks there.
        late.start()
        assert late_reached.wait(10), "late opener never reached access or lock"
        result = real_quarantine(path, **kwargs)
        assert result is not None and not db.exists()
        quarantined.set()
        if access_reached.is_set():
            assert missing_observed.wait(10), "late opener never probed the missing DB"
        return result

    def connect_then_release(self):
        result = real_connect(self)
        if not is_late():
            recreated.set()
        return result

    monkeypatch.setattr(repair.os, "access", gated_access)
    monkeypatch.setattr(hs, "quarantine_cross_process_lock", signalling_lock)
    monkeypatch.setattr(hs, "quarantine_invalid_state_db", quarantine_then_release)
    monkeypatch.setattr(hs.SessionDB, "_connect_and_init_with_lock_patience", connect_then_release)

    first = threading.Thread(target=worker, args=(0,), name="first-opener")
    first.start()
    first.join(30)
    if late.ident is not None:
        late.join(30)
    assert not first.is_alive() and not late.is_alive()
    assert errors == [None, None], f"concurrent open raised: {errors}, access: {observations}"
    assert observations == [], f"file preflight escaped the quarantine lock: {observations}"
    backups = list(tmp_path.glob("state.db.zeroed-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == bytes(4096)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT id FROM sessions ORDER BY id").fetchall() == [
            ("opener-0",), ("opener-1",)
        ]


@pytest.mark.platforms("posix")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses file permission checks",
)
def test_permission_repair_keeps_concurrent_sessions_in_canonical_db(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = home / "state.db"
    db.write_bytes(bytes(4096))
    db.chmod(0o200)
    assert not os.access(db, os.R_OK)

    replacement_created = threading.Event()
    stale_probe = threading.Event()
    first_finished = threading.Event()
    real_secure = hs._secure_state_db_files
    real_lock = hs.quarantine_cross_process_lock
    real_invalid = dbfile.has_invalid_sqlite_header_preopen
    errors: list[BaseException | None] = [None, None]

    def is_late():
        return threading.current_thread().name == "permission-late-opener"

    def secure_then_schedule(path, **kwargs):
        result = real_secure(path, **kwargs)
        if not is_late() and kwargs.get("create_main") and not replacement_created.is_set():
            replacement_created.set()
            # Choose the vulnerable interleaving only if startup is unlocked.
            # With serialization, the late opener must wait for initialization.
            with real_lock(path, timeout=0) as acquired:
                startup_unlocked = acquired
            late.start()
            if startup_unlocked:
                assert stale_probe.wait(10), "late opener never inspected the empty replacement"
        return result

    def probe_then_allow_first_commit(path, *args, **kwargs):
        result = real_invalid(path, *args, **kwargs)
        if is_late() and result:
            stale_probe.set()
            assert first_finished.wait(10), "first opener never committed its session"
        return result

    def worker(idx):
        try:
            sdb = hs.SessionDB(db_path=db)
            try:
                sdb.create_session(session_id=f"permission-opener-{idx}", source="test")
            finally:
                sdb.close()
        except BaseException as exc:
            errors[idx] = exc
        finally:
            if idx == 0:
                first_finished.set()

    late = threading.Thread(target=worker, args=(1,), name="permission-late-opener")
    monkeypatch.setattr(hs, "_secure_state_db_files", secure_then_schedule)
    monkeypatch.setattr(dbfile, "has_invalid_sqlite_header_preopen", probe_then_allow_first_commit)

    first = threading.Thread(target=worker, args=(0,), name="permission-first-opener")
    first.start()
    first.join(30)
    if late.ident is not None:
        late.join(30)
    assert not first.is_alive() and not late.is_alive()
    assert replacement_created.is_set(), "first opener never created the replacement"
    assert errors == [None, None], f"permission-repair startup raised: {errors}"
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT id FROM sessions ORDER BY id").fetchall() == [
            ("permission-opener-0",), ("permission-opener-1",)
        ]
    backups = list(home.glob("state.db.*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == bytes(4096)
