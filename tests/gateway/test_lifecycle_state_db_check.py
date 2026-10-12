"""An unclean gateway death must trigger a state.db integrity check.

Regression for the 2026-08-31 incident. ``state.db`` was corrupt from
2026-08-26 evening (a SIGKILL landed on a gateway mid-WAL-checkpoint during a
``--replace`` restart storm), but nothing checked the file. The damage sat in
old, rarely-read session rows for 3.5 days until a Desktop read tripped over
it on 2026-08-30 17:15 and surfaced as "Session not found".

``record_startup`` already detects the unclean exit and logs "SIGKILL / OOM /
VM death" — it just never looked at the database that death may have torn.
The check is gated on the unclean exit; a clean boot must not scan. Where it
runs depends on the on-disk journal mode: a WAL store is scanned off the
readiness path (a WAL reader never blocks writers), while a rollback-journal
store is scanned synchronously before any writer exists, because its reader
holds SHARED for the whole scan and fails every writer with "database is locked".
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from gateway.lifecycle_ledger import (
    check_state_db_integrity,
    get_lifecycle_sentinel_path,
    record_startup,
)

_DEAD_PID = 2 ** 22 + 12345  # beyond default pid_max; never alive


def _write_sentinel(home: Path, phase: str = "running") -> None:
    path = get_lifecycle_sentinel_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "phase": phase,
            "pid": _DEAD_PID,
            "start_time": 1000.0,
            "started_at": "2026-08-26T23:56:45+00:00",
        }),
        encoding="utf-8",
    )


def _make_state_db(home: Path, *, corrupt: bool, journal: str = "wal") -> Path:
    """Build a real SQLite file in *journal* mode, optionally with a genuinely torn b-tree page."""
    path = home / "state.db"
    conn = sqlite3.connect(path)
    assert conn.execute(f"PRAGMA journal_mode={journal}").fetchone()[0] == journal
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany(
        "INSERT INTO sessions (v) VALUES (?)", [(f"row-{i}" * 40,) for i in range(4000)]
    )
    conn.commit()
    conn.close()
    if corrupt:
        with open(path, "r+b") as handle:
            handle.seek(4096 * 6)
            handle.write(b"\xEF" * 4096)
    return path


def _exit_diag_records(home: Path) -> list:
    log = home / "logs" / "gateway-exit-diag.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]



@pytest.fixture(autouse=True)
def integrity_workers(monkeypatch):
    """Record spawned checker threads; calling the fixture joins them."""
    import agent.memory_provider as provider

    workers = []
    spawn = provider.spawn_context_thread

    def track(*args, **kwargs):
        worker = spawn(*args, **kwargs)
        workers.append(worker)
        return worker

    def wait():
        for worker in workers:
            worker.join(timeout=5)
            assert not worker.is_alive(), "integrity checker did not finish"
        return len(workers)

    monkeypatch.setattr(provider, "spawn_context_thread", track)
    yield wait
    wait()


def _armed_handle(monkeypatch):
    """A real, thread-less StartupWatchdogHandle installed as the module singleton."""
    import hermes_startup_watchdog as sw

    handle = sw.StartupWatchdogHandle(timeout_s=300.0, exit_code=75)
    monkeypatch.setattr(sw, "_handle", handle)
    return handle


# ── the checker itself ──────────────────────────────────────────────────────


def test_checker_passes_a_healthy_store(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=False)
    assert check_state_db_integrity(home=tmp_path) == "ok"


def test_checker_reports_a_torn_btree_page(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=True)
    verdict = check_state_db_integrity(home=tmp_path)
    assert verdict != "ok"
    assert "btreeInitPage" in verdict or "malformed" in verdict.lower()


def test_checker_tolerates_a_missing_store(tmp_path: Path) -> None:
    assert check_state_db_integrity(home=tmp_path) == "absent"


# ── wiring into the unclean-exit path ───────────────────────────────────────


@pytest.mark.parametrize("journal", ["wal", "delete"])
@pytest.mark.parametrize("corrupt", [False, True])
def test_unclean_exit_persists_the_verdict_in_either_journal_mode(
    tmp_path: Path, integrity_workers, caplog, journal, corrupt
) -> None:
    _make_state_db(tmp_path, corrupt=corrupt, journal=journal)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path)

    assert evidence is not None
    assert integrity_workers() == (1 if journal == "wal" else 0)
    records = _exit_diag_records(tmp_path)
    assert len(records) == (2 if journal == "wal" else 1)
    verdict = records[-1]["state_db_integrity"]
    if journal == "delete":
        assert evidence["state_db_integrity"] == verdict, "synchronous path returns its verdict"
    failed = any(r.levelno == logging.ERROR and "FAILED integrity check" in r.message for r in caplog.records)
    assert (verdict not in ("ok", "absent"), failed) == (corrupt, corrupt)


@pytest.mark.parametrize("journal", ["wal", "delete"])
def test_clean_exit_does_not_pay_for_the_check(tmp_path: Path, monkeypatch, integrity_workers, journal) -> None:
    """A clean boot must not scan the store — that is the whole cost gate."""
    _make_state_db(tmp_path, corrupt=True, journal=journal)
    _write_sentinel(tmp_path, phase="exited")

    called = []
    import gateway.lifecycle_ledger as ledger

    monkeypatch.setattr(
        ledger, "check_state_db_integrity", lambda **kw: called.append(1) or "ok"
    )
    assert record_startup(home=tmp_path) is None
    assert integrity_workers() == 0
    assert not called, "integrity check ran on a clean boot"


def _held_check(monkeypatch, verdict=None, *, on_start=None):
    """Replace the checker with one that blocks until released; returns (started, release, calls)."""
    import gateway.lifecycle_ledger as ledger

    real = ledger.check_state_db_integrity
    started, release, calls = threading.Event(), threading.Event(), []

    def held(**kw):
        calls.append(kw)
        if on_start is not None:
            on_start()
        started.set()
        assert release.wait(timeout=5), "test did not release the checker"
        return real(**kw) if verdict is None else verdict

    monkeypatch.setattr(ledger, "check_state_db_integrity", held)
    return started, release, calls


def _boot_in_thread(home: Path):
    """Run ``record_startup`` on its own thread so a regression that waits stays bounded."""
    returned, result = threading.Event(), []

    def boot():
        result.append(record_startup(home=home))
        returned.set()

    caller = threading.Thread(target=boot, name="test-startup-caller", daemon=True)
    caller.start()
    return caller, returned, result


@pytest.mark.parametrize("verdict", ["ok", "btree page is corrupt"])
def test_wal_startup_returns_while_integrity_check_is_running(
    tmp_path: Path, monkeypatch, integrity_workers, caplog, verdict
) -> None:
    """WAL: a held check cannot hold up readiness, its verdict, or the unclean warning."""
    handle = _armed_handle(monkeypatch)
    _make_state_db(tmp_path, corrupt=False, journal="wal")
    _write_sentinel(tmp_path)
    before_scan = []
    started, release, calls = _held_check(
        monkeypatch, verdict, on_start=lambda: before_scan.extend(_exit_diag_records(tmp_path))
    )

    caller, returned, result = _boot_in_thread(tmp_path)
    try:
        assert started.wait(timeout=2), "integrity check was not started"
        assert returned.wait(timeout=2), "startup waited on the integrity check"
        assert not release.is_set()
        sentinel = json.loads(get_lifecycle_sentinel_path(tmp_path).read_text())
        assert sentinel["phase"] == "running" and sentinel["prior_unclean_exit"] is True
        assert result[0]["prior_pid"] == _DEAD_PID
        assert "state_db_integrity" not in result[0]
        assert len(before_scan) == 1, "unclean-exit evidence was not persisted before the scan"
        pending = before_scan[0]
        assert pending["tag"] == "gateway.previous_unclean_exit"
        assert pending["prior_pid"] == _DEAD_PID
        assert pending["state_db_integrity"] == "pending"
        assert _exit_diag_records(tmp_path) == before_scan
        assert "exited UNCLEANLY" in caplog.text
        assert handle._lease_count == 0, "background work masked startup liveness"
    finally:
        release.set()
        caller.join(timeout=5)
        integrity_workers()

    assert calls == [{"home": tmp_path, "renew_startup_lease": False}]
    records = _exit_diag_records(tmp_path)
    assert len(records) == 2
    assert records[0] == pending, "completion rewrote the original evidence"
    record = records[-1]
    assert record["tag"] == "gateway.previous_unclean_exit"
    assert record["prior_pid"] == _DEAD_PID
    assert record["state_db_integrity"] == verdict
    errors = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("FAILED integrity check" in msg for msg in errors) == (verdict != "ok")
    assert handle._lease_count == 0


def test_rollback_journal_scan_finishes_before_startup_lets_writers_in(
    tmp_path: Path, monkeypatch, integrity_workers
) -> None:
    """DELETE mode: a rollback reader holds SHARED for the whole scan, so a background scan
    failed SessionDB writes with "database is locked". The scan must be over by the time
    ``record_startup`` returns, and a writer that cannot wait at all must then commit."""
    path = _make_state_db(tmp_path, corrupt=False, journal="delete")
    _write_sentinel(tmp_path)
    started, release, calls = _held_check(monkeypatch)

    caller, returned, result = _boot_in_thread(tmp_path)
    try:
        assert started.wait(timeout=2), "integrity check was not started"
        # Event-gated: the checker cannot finish until released, so a return here is a scan
        # left running beside the writers this boot is about to start.
        assert not returned.is_set(), "startup returned with the rollback-journal scan still running"
    finally:
        release.set()
        caller.join(timeout=5)
    assert returned.is_set()
    assert integrity_workers() == 0
    assert calls == [{"home": tmp_path, "renew_startup_lease": True}]
    assert result[0]["state_db_integrity"] == "ok"

    writer = sqlite3.connect(path, timeout=0)
    try:
        writer.execute("INSERT INTO sessions (v) VALUES ('after-startup')")
        writer.commit()
    finally:
        writer.close()


def test_rollback_journal_check_renews_the_startup_lease_while_sqlite_progresses(
    tmp_path: Path, monkeypatch
) -> None:
    """The synchronous scan is startup work: a 37 GB store needs ~4200 s of quick_check and one
    900 s entry lease cannot cover it (#115542), so it renews a phase-owned lease from SQLite's
    progress handler — without converting corruption into success or aborting the PRAGMA."""
    import gateway.lifecycle_ledger as ledger

    handle = _armed_handle(monkeypatch)
    # Renew on every handler tick so the renewal count is deterministic, not clock-bound.
    monkeypatch.setattr(ledger, "_INTEGRITY_CHECK_LEASE_RENEW_S", 0.0)
    monkeypatch.setattr(ledger, "_INTEGRITY_CHECK_PROGRESS_OPS", 1_000)
    _make_state_db(tmp_path, corrupt=False, journal="delete")
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path)

    assert evidence is not None and evidence["state_db_integrity"] == "ok"
    assert handle._lease_phase == "state_db_unclean_integrity_check"
    assert handle._lease_count > 1, "lease was taken once at entry and never renewed"

    torn = tmp_path / "torn"
    torn.mkdir()
    _make_state_db(torn, corrupt=True, journal="delete")
    verdict = check_state_db_integrity(home=torn, renew_startup_lease=True)
    assert verdict not in ("ok", "absent") and not verdict.startswith("check-failed")
