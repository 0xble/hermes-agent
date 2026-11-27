"""Concurrent holder checks share a bounded scan and recover after its timeout."""

import gc
import os
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import hermes_state_dbfile as dbfile


@pytest.mark.platforms("macos")
def test_healthy_sidecar_closed_later_in_shared_scan_is_not_retired(tmp_path, monkeypatch):
    sidecar = tmp_path / "state.db-wal"
    sidecar.write_bytes(b"healthy")
    info = sidecar.stat()

    def targets():
        yield 123, 10, str(sidecar), (info.st_dev, info.st_ino)
        sidecar.unlink()  # SQLite's normal last-connection close during the scan.

    monkeypatch.setattr(dbfile, "_iter_darwin_fd_targets", targets)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_WORKER", None)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_RETRY_AT", 0.0)
    assert dbfile.iter_deleted_sqlite_sidecar_holders(tmp_path / "state.db") == []


@pytest.mark.platforms("macos")
def test_self_sidecar_descriptor_reuse_is_not_retired(tmp_path, monkeypatch):
    import fcntl

    path = str(tmp_path / "state.db-wal")
    identities = iter([SimpleNamespace(st_dev=1, st_ino=2), SimpleNamespace(st_dev=1, st_ino=3)])
    monkeypatch.setattr(dbfile.os, "listdir", lambda _: ["10"])
    monkeypatch.setattr(fcntl, "fcntl", lambda *args: path.encode() + b"\0")
    monkeypatch.setattr(dbfile.os, "fstat", lambda _: next(identities))
    assert dbfile._darwin_self_sidecar_holders({path.casefold(): path}) == []


@pytest.mark.platforms("macos")
def test_concurrent_holder_checks_do_not_accumulate_stuck_scans(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked_targets():
        calls.append(1)
        entered.set()
        release.wait(10)
        return
        yield  # generator whose first descriptor read cannot finish

    monkeypatch.setattr(dbfile, "_iter_darwin_fd_targets", blocked_targets)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_COOLDOWN_SECONDS", 0.2)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_WORKER", None)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_RETRY_AT", 0.0)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(dbfile.iter_deleted_sqlite_sidecar_holders,
                                    [tmp_path / f"state-{i}.db" for i in range(8)]))
        assert entered.is_set()
        assert results == [[]] * 8
        assert len(calls) == 1
        assert dbfile.iter_deleted_sqlite_sidecar_holders(tmp_path / "retry.db") == []
        assert len(calls) == 1
    finally:
        release.set()
    time.sleep(0.3)
    assert dbfile.iter_deleted_sqlite_sidecar_holders(tmp_path / "recovered.db") == []
    assert len(calls) == 2


@pytest.mark.platforms("macos")
def test_retired_self_sidecar_is_detected_while_foreign_scan_is_stalled(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    path = tmp_path / "state.db"

    def blocked_targets():
        entered.set()
        release.wait(10)
        return
        yield

    monkeypatch.setattr(dbfile, "_iter_darwin_fd_targets", blocked_targets)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_WORKER", None)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_RETRY_AT", 0.0)
    try:
        assert dbfile.iter_deleted_sqlite_sidecar_holders(path) == []
        assert entered.is_set()
        sidecar = tmp_path / "state.db-wal"
        sidecar.write_bytes(b"retired generation")
        with sidecar.open("rb"):
            sidecar.unlink()
            holders = dbfile.iter_deleted_sqlite_sidecar_holders(path)
            assert any(pid == os.getpid() for pid, _ in holders)
            other = tmp_path / "other.db"
            assert dbfile.iter_deleted_sqlite_sidecar_holders(other) == []
    finally:
        release.set()


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("scan_error", [OSError, AssertionError], ids=["os-error", "guard-error"])
def test_scan_failure_does_not_retain_callers(tmp_path, monkeypatch, scan_error):
    from hermes_state import SessionDB

    scan_failures = []

    def failed_targets():
        scan_failures.append(True)
        raise scan_error("descriptor scan unavailable")

    monkeypatch.setattr(dbfile, "_iter_darwin_fd_targets", failed_targets)
    monkeypatch.setattr(dbfile, "_darwin_self_sidecar_holders", lambda watched: [])
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_WORKER", None)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_RETRY_AT", 0.0)

    class Context:
        pass

    def open_handle():
        context = Context()
        return SessionDB(tmp_path / "state.db"), weakref.ref(context)

    handle, context_ref = open_handle()
    try:
        assert scan_failures
        handle.create_session("after-scan-error", source="fixture", model="m")
        assert handle.get_session("after-scan-error") is not None
        gc.collect()
        assert context_ref() is None
    finally:
        handle.close()

