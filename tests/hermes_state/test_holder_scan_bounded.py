"""The pre-open deleted-WAL holder scan is host-wide: a clean verdict must be reused, not repeated.

On macOS it walks every descriptor of every process through libproc; a boot's burst of opens
repeated it back to back. A clean verdict is reused while both sidecars keep their identity, and
never when a sidecar is missing (that is when an opener could mint a replacement WAL).
"""

from __future__ import annotations

import os

import pytest

import hermes_state_dbfile as dbfile
from hermes_state import DeletedWalGenerationError


@pytest.fixture(autouse=True)
def _fresh_scan_state(monkeypatch):
    monkeypatch.setattr(dbfile, "_holder_scan_clean", {})
    monkeypatch.setattr(dbfile, "_holder_scan_path_locks", {})


def _sidecars(tmp_path):
    wal, shm = tmp_path / "state.db-wal", tmp_path / "state.db-shm"
    wal.write_bytes(b"generation-1")
    shm.write_bytes(b"shm")
    return wal, shm


def test_clean_verdict_is_reused_until_a_sidecar_is_replaced(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    wal, _shm = _sidecars(tmp_path)
    scans = []
    monkeypatch.setattr(dbfile, "iter_deleted_sqlite_sidecar_holders", lambda path: scans.append(path) or [])

    dbfile.refuse_deleted_wal_generation(db)
    dbfile.refuse_deleted_wal_generation(db)
    assert len(scans) == 1

    # A retired generation is one some process still holds open, so its inode cannot be reused.
    held = os.open(wal, os.O_RDONLY)
    try:
        wal.unlink()
        wal.write_bytes(b"generation-2")
        monkeypatch.setattr(dbfile, "iter_deleted_sqlite_sidecar_holders", lambda path: [(4242, str(wal))])
        with pytest.raises(DeletedWalGenerationError):
            dbfile.refuse_deleted_wal_generation(db)
        with pytest.raises(DeletedWalGenerationError):  # a dirty verdict is never cached
            dbfile.refuse_deleted_wal_generation(db)
    finally:
        os.close(held)


def test_missing_sidecar_always_rescans(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    wal, _shm = _sidecars(tmp_path)
    scans = []
    monkeypatch.setattr(dbfile, "iter_deleted_sqlite_sidecar_holders", lambda path: scans.append(path) or [])
    dbfile.refuse_deleted_wal_generation(db)
    wal.unlink()  # the next opener would mint a WAL: the cached verdict must not cover it
    dbfile.refuse_deleted_wal_generation(db)
    dbfile.refuse_deleted_wal_generation(db)
    assert len(scans) == 3
