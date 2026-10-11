"""The pre-open deleted-WAL holder scan is host-wide: it must be reused, single-flight, and bounded.

On macOS it walks every descriptor of every process through libproc. Repeated per open, a boot's
burst of opens queued behind it, and one vnode stuck in the kernel left every opener in
uninterruptible sleep. A clean verdict is reused while the sidecars keep their identity; a scan
that does not finish in time fails open, like the scan itself does on error.
"""

from __future__ import annotations

import threading

import pytest

import hermes_state_dbfile as dbfile
from hermes_state import DeletedWalGenerationError


@pytest.fixture(autouse=True)
def _fresh_scan_state(monkeypatch):
    monkeypatch.setattr(dbfile, "_holder_scan_clean", {})
    monkeypatch.setattr(dbfile, "_holder_scan_inflight", {})


def test_clean_verdict_is_reused_until_a_sidecar_changes(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    wal = tmp_path / "state.db-wal"
    wal.write_bytes(b"generation-1")
    scans = []
    monkeypatch.setattr(dbfile, "iter_deleted_sqlite_sidecar_holders", lambda path: scans.append(path) or [])

    dbfile.refuse_deleted_wal_generation(db)
    dbfile.refuse_deleted_wal_generation(db)
    assert len(scans) == 1

    wal.unlink()
    wal.write_bytes(b"generation-2")  # replaced: a retired generation may now exist
    holders = [(4242, str(wal))]
    monkeypatch.setattr(dbfile, "iter_deleted_sqlite_sidecar_holders", lambda path: holders)
    with pytest.raises(DeletedWalGenerationError):
        dbfile.refuse_deleted_wal_generation(db)
    with pytest.raises(DeletedWalGenerationError):  # a dirty verdict is never cached
        dbfile.refuse_deleted_wal_generation(db)


def test_wedged_scan_fails_open_and_is_not_duplicated(tmp_path, monkeypatch):
    release = threading.Event()
    started = []

    def wedged(path):
        started.append(path)
        release.wait(10)
        return []

    monkeypatch.setattr(dbfile, "iter_deleted_sqlite_sidecar_holders", wedged)
    monkeypatch.setattr(dbfile, "_HOLDER_SCAN_WAIT_S", 0.05)
    try:
        dbfile.refuse_deleted_wal_generation(tmp_path / "state.db")
        dbfile.refuse_deleted_wal_generation(tmp_path / "state.db")
        assert len(started) == 1  # the second opener waited on the in-flight scan
    finally:
        release.set()
