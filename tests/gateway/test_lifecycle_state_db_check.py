"""An unclean gateway death must trigger a state.db integrity check.

Regression for the 2026-08-31 incident. ``state.db`` was corrupt from
2026-08-26 evening (a SIGKILL landed on a gateway mid-WAL-checkpoint during a
``--replace`` restart storm), but nothing checked the file. The damage sat in
old, rarely-read session rows for 3.5 days until a Desktop read tripped over
it on 2026-08-30 17:15 and surfaced as "Session not found".

``record_startup`` already detects the unclean exit and logs "SIGKILL / OOM /
VM death" — it just never looked at the database that death may have torn.
The check is gated on the unclean exit; a clean boot must not scan. WAL scans
are deferred until after startup so the forensic check cannot gate readiness.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.lifecycle_ledger import (
    check_state_db_integrity,
    get_lifecycle_sentinel_path,
    record_startup,
    run_deferred_integrity_check,
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


def _make_state_db(
    home: Path, *, corrupt: bool, journal: str = "wal", journal_mode: str | None = None,
) -> Path:
    """Build a real SQLite file in either journal mode, optionally corrupting a b-tree page."""
    selected_journal = journal if journal_mode is None else journal_mode
    path = home / "state.db"
    conn = sqlite3.connect(path)
    if selected_journal != "delete":
        assert conn.execute(f"PRAGMA journal_mode={selected_journal}").fetchone()[0] == selected_journal
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


def _make_multi_page_state_db(home: Path, rows: int = 300_000) -> Path:
    """A store large enough for deadline/progress-handler coverage."""
    path = home / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany(
        "INSERT INTO sessions (v) VALUES (?)", [(f"row-{i}" * 40,) for i in range(rows)]
    )
    conn.commit()
    conn.close()
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


def test_first_header_probe_failure_keeps_delete_scan_before_claim(tmp_path: Path, monkeypatch) -> None:
    """A failed first WAL probe must not defer a DELETE scan until after claiming."""
    _write_sentinel(tmp_path)
    import gateway.lifecycle_ledger as ledger

    order = []
    probe_calls = []

    def probe(_home):
        probe_calls.append(True)
        if len(probe_calls) == 1:
            raise RuntimeError("header read failed")
        return False

    monkeypatch.setattr(ledger, "_state_db_is_wal", probe)
    monkeypatch.setattr(ledger, "_report_unclean_exit", lambda *args, **kwargs: order.append("scan"))
    monkeypatch.setattr(ledger, "_write_sentinel", lambda *args, **kwargs: order.append("claim"))

    record_startup(home=tmp_path)

    assert order == ["scan", "claim"]
    assert len(probe_calls) == 1


def test_checker_tolerates_a_missing_store(tmp_path: Path) -> None:
    assert check_state_db_integrity(home=tmp_path) == "absent"


def _foreign_exclusive_acquired(path: Path) -> bool:
    """Try SQLite's exclusive WAL reset from a separate process."""
    child = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
try:
    conn.execute(\"PRAGMA locking_mode=EXCLUSIVE\")
    conn.execute(\"BEGIN IMMEDIATE\")
    conn.execute(\"ROLLBACK\")
    print(\"EXCLUSIVE_ACQUIRED\")
except sqlite3.OperationalError:
    print(\"REFUSED\")
finally:
    conn.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", child, str(path)], capture_output=True, text=True, check=True,
    )
    return "EXCLUSIVE_ACQUIRED" in result.stdout


def test_wal_scan_stays_lock_visible_during_sessiondb_startup(
    tmp_path: Path, monkeypatch
) -> None:
    """A live WAL scan must remain visible while SessionDB runs its preopen probe.

    The child process is the observation boundary: if the preopen probe closes a
    descriptor for an untracked checker, SQLite's POSIX lock disappears and the
    child can take the exclusive reset lock.
    """
    import gateway.lifecycle_ledger as ledger
    import hermes_state_dbfile
    from hermes_state import SessionDB

    path = tmp_path / "state.db"
    seed = SessionDB(db_path=path)
    seed.close()
    scan_started, release, result = threading.Event(), threading.Event(), []

    class HeldQuickCheckConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if str(sql).lower().startswith("pragma quick_check"):
                scan_started.set()
                assert release.wait(timeout=5), "test did not release the WAL scan"
            return super().execute(sql, parameters)

    real_connect = sqlite3.connect

    def connect_for_scan(database, *args, **kwargs):
        kwargs["factory"] = HeldQuickCheckConnection
        return real_connect(database, *args, **kwargs)

    # The current implementation calls ledger.sqlite3.connect directly. Keep
    # the same held connection when the fixed implementation uses the tracked
    # state-db helper, without weakening that helper's real registration.
    monkeypatch.setattr(ledger, "sqlite3", SimpleNamespace(connect=connect_for_scan))
    real_tracked_connect = hermes_state_dbfile._connect_tracked_db

    def tracked_connect_for_scan(database, *args, **kwargs):
        kwargs["factory"] = HeldQuickCheckConnection
        return real_tracked_connect(database, *args, **kwargs)

    monkeypatch.setattr(hermes_state_dbfile, "_connect_tracked_db", tracked_connect_for_scan)

    worker = threading.Thread(
        target=lambda: result.append(check_state_db_integrity(home=tmp_path)),
        name="held-wal-integrity-check",
        daemon=True,
    )
    worker.start()
    assert scan_started.wait(timeout=5), "WAL integrity scan did not reach quick_check"
    from hermes_cli.sqlite_safe_read import has_live_connection

    assert has_live_connection(path), "WAL scan connection is not registered for preopen safety"

    writer = SessionDB(db_path=path)
    try:
        writer.create_session("lock-probe", source="test")
    finally:
        release.set()
        worker.join(timeout=5)

    try:
        assert not _foreign_exclusive_acquired(path), (
            "a foreign process acquired the exclusive WAL reset lock while the scan and writer were live"
        )
    finally:
        writer.close()

    assert not worker.is_alive(), "held WAL integrity scan did not finish"
    assert result == ["ok"]


# ── wiring into the unclean-exit path ───────────────────────────────────────


@pytest.mark.parametrize("journal", ["wal", "delete"])
@pytest.mark.parametrize("corrupt", [False, True])
def test_unclean_exit_persists_the_verdict_in_either_journal_mode(
    tmp_path: Path, integrity_workers, caplog, journal, corrupt
) -> None:
    _make_state_db(tmp_path, corrupt=corrupt, journal=journal)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path, defer_integrity_check=False)

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


def test_deferred_startup_never_runs_the_check_inline(tmp_path: Path, monkeypatch) -> None:
    """DEFERRAL IS THE DEFAULT: the boot entry point must return WITHOUT the forensic
    scan completing, and the exit-diag record must record that the verdict is pending.
    The blocking stub below never returns unless the deferred path wrongly runs it."""
    import gateway.lifecycle_ledger as ledger

    started = threading.Event()
    released = threading.Event()

    def _blocking_check(**kwargs):
        started.set()
        released.wait(10)  # never set by the caller -> proves the boot path did not run it
        return "ok"

    monkeypatch.setattr(ledger, "check_state_db_integrity", _blocking_check)
    _make_state_db(tmp_path, corrupt=False, journal="wal")
    _write_sentinel(tmp_path)

    begin = time.monotonic()
    evidence = record_startup(home=tmp_path)  # default: defer_integrity_check=True
    elapsed = time.monotonic() - begin

    assert evidence is not None
    assert evidence["state_db_integrity"] == "deferred"
    assert not started.is_set(), "the inline integrity scan ran on the deferred boot path"
    assert elapsed < 5.0
    record = _exit_diag_records(tmp_path)[0]
    assert record["state_db_integrity"] == "deferred"
    assert record["tag"] == "gateway.previous_unclean_exit"


def test_deferred_scan_appends_a_second_record_and_returns_the_verdict(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=True, journal_mode="wal")
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path, defer_integrity_check=True)
    assert evidence is not None and evidence["state_db_integrity"] == "deferred"

    verdict = run_deferred_integrity_check(evidence, home=tmp_path, deadline_s=60.0)

    records = _exit_diag_records(tmp_path)
    assert [r["tag"] for r in records] == [
        "gateway.previous_unclean_exit", "gateway.state_db_integrity"]
    deferred = records[1]
    assert deferred["deferred"] is True
    assert deferred["state_db_integrity"] == verdict
    assert deferred["prior_pid"] == _DEAD_PID
    assert verdict != "ok"
    assert evidence["state_db_integrity"] == verdict


def test_deferred_scan_never_raises(tmp_path: Path, monkeypatch) -> None:
    import gateway.lifecycle_ledger as ledger

    _write_sentinel(tmp_path)
    evidence = record_startup(home=tmp_path, defer_integrity_check=True)

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ledger, "check_state_db_integrity", _boom)

    assert run_deferred_integrity_check(evidence, home=tmp_path).startswith("check-failed")


# ── rollback-journal stores: a background scan would block live writers ──────
# In journal_mode=delete a quick_check holds a SHARED lock for its whole run, so every
# live gateway commit waits on it; WAL readers never block writers.


def _spy_on_scan(monkeypatch) -> list:
    """Record every scan while still running the real one."""
    import gateway.lifecycle_ledger as ledger

    calls: list = []
    real = ledger.check_state_db_integrity

    def _spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(ledger, "check_state_db_integrity", _spy)
    return calls


def test_deferred_scan_still_runs_on_a_wal_store(tmp_path: Path, monkeypatch) -> None:
    _make_state_db(tmp_path, corrupt=False, journal_mode="wal")
    calls = _spy_on_scan(monkeypatch)

    verdict = run_deferred_integrity_check({"prior_pid": _DEAD_PID}, home=tmp_path, deadline_s=60.0)

    assert verdict == "ok"
    assert len(calls) == 1
    assert _exit_diag_records(tmp_path)[-1]["state_db_integrity"] == "ok"


def test_deferred_scan_skips_a_rollback_journal_store(tmp_path: Path, monkeypatch, caplog) -> None:
    path = _make_state_db(tmp_path, corrupt=False, journal="delete")
    calls = _spy_on_scan(monkeypatch)
    evidence = {"prior_pid": _DEAD_PID}

    with caplog.at_level(logging.WARNING, logger="gateway.lifecycle_ledger"):
        verdict = run_deferred_integrity_check(evidence, home=tmp_path, deadline_s=60.0)

    assert not calls, "quick_check ran beside live traffic on a rollback-journal store"
    assert verdict == ("skipped: rollback-journal store (journal_mode=delete), "
                       "scan would block live writes")
    assert evidence["state_db_integrity"] == verdict
    record = _exit_diag_records(tmp_path)[-1]
    assert record["tag"] == "gateway.state_db_integrity"
    assert record["state_db_integrity"] == verdict
    assert record["prior_pid"] == _DEAD_PID
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "unclean" in warnings[0].lower() and "skipped" in warnings[0].lower()
    assert str(path) in warnings[0] and "PRAGMA quick_check" in warnings[0]
    # The journal-mode probe left no lock behind: a live writer commits without waiting.
    writer = sqlite3.connect(path, timeout=0)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO sessions (v) VALUES ('live')")
        writer.commit()
    finally:
        writer.close()


def test_deferred_scan_skips_when_the_journal_mode_is_undeterminable(tmp_path: Path, monkeypatch) -> None:
    import hermes_state_wal

    _make_state_db(tmp_path, corrupt=False, journal_mode="wal")
    calls = _spy_on_scan(monkeypatch)

    def _unreadable(conn):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(hermes_state_wal, "_on_disk_journal_mode", _unreadable)

    verdict = run_deferred_integrity_check({"prior_pid": _DEAD_PID}, home=tmp_path, deadline_s=60.0)

    assert not calls
    assert verdict == ("skipped: rollback-journal store (journal_mode=unknown), "
                       "scan would block live writes")
    assert _exit_diag_records(tmp_path)[-1]["state_db_integrity"] == verdict


def test_deferred_scan_of_an_absent_store_is_unchanged(tmp_path: Path) -> None:
    verdict = run_deferred_integrity_check({"prior_pid": _DEAD_PID}, home=tmp_path, deadline_s=60.0)

    assert verdict == "absent"
    assert _exit_diag_records(tmp_path)[-1]["state_db_integrity"] == "absent"
    assert not (tmp_path / "state.db").exists()


# ── bounded scan: a deadline aborts through the progress handler ─────────────


def test_deadline_bounds_the_scan(tmp_path: Path, monkeypatch) -> None:
    import gateway.lifecycle_ledger as ledger

    _make_multi_page_state_db(tmp_path)
    monkeypatch.setattr(ledger, "_INTEGRITY_CHECK_PROGRESS_OPS", 1_000)

    verdict = check_state_db_integrity(home=tmp_path, deadline_s=0.01)

    assert verdict.startswith("check-incomplete: exceeded"), verdict


def test_bounded_background_scan_never_renews_the_startup_lease(tmp_path: Path, monkeypatch) -> None:
    """The fork guard: only the synchronous startup scan may renew the startup-watchdog
    lease. A deadline-bounded background scan renewing it would mask a wedged startup."""
    import gateway.lifecycle_ledger as ledger

    handle = _armed_handle(monkeypatch)
    monkeypatch.setattr(ledger, "_INTEGRITY_CHECK_LEASE_RENEW_S", 0.0)
    monkeypatch.setattr(ledger, "_INTEGRITY_CHECK_PROGRESS_OPS", 1_000)
    _make_state_db(tmp_path, corrupt=False)

    assert check_state_db_integrity(home=tmp_path, deadline_s=60.0) == "ok"
    assert getattr(handle, "_lease_phase", None) != "state_db_unclean_integrity_check"


def test_no_deadline_keeps_the_historic_verdict_contract(tmp_path: Path, monkeypatch) -> None:
    """``deadline_s=None`` must be byte-for-byte today's behaviour: the handler never aborts."""
    import gateway.lifecycle_ledger as ledger

    _make_multi_page_state_db(tmp_path)
    monkeypatch.setattr(ledger, "_INTEGRITY_CHECK_PROGRESS_OPS", 1_000)

    assert check_state_db_integrity(home=tmp_path) == "ok"
    assert check_state_db_integrity(home=tmp_path, deadline_s=None) == "ok"


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
        result.append(record_startup(home=home, defer_integrity_check=False))
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
        # The old life must remain the sentinel owner while the synchronous scan is held;
        # otherwise this process's watchdog exit could rewrite the prior-life evidence.
        sentinel = json.loads(get_lifecycle_sentinel_path(tmp_path).read_text())
        assert sentinel["phase"] == "running"
        assert sentinel["pid"] == _DEAD_PID
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

    evidence = record_startup(home=tmp_path, defer_integrity_check=False)

    assert evidence is not None and evidence["state_db_integrity"] == "ok"
    assert handle._lease_phase == "state_db_unclean_integrity_check"
    assert handle._lease_count > 1, "lease was taken once at entry and never renewed"

    torn = tmp_path / "torn"
    torn.mkdir()
    _make_state_db(torn, corrupt=True, journal="delete")
    verdict = check_state_db_integrity(home=torn, renew_startup_lease=True)
    assert verdict not in ("ok", "absent") and not verdict.startswith("check-failed")


def test_unclean_exit_check_keeps_the_verdict_contract_under_the_lease(
    tmp_path: Path, monkeypatch
) -> None:
    """The progress handler must never convert corruption into success or abort the PRAGMA:
    a torn page still yields the first complaint, and the phase is still on record."""
    handle = _armed_handle(monkeypatch)
    _make_state_db(tmp_path, corrupt=True)

    verdict = check_state_db_integrity(home=tmp_path, renew_startup_lease=True)

    assert verdict not in ("ok", "absent") and not verdict.startswith("check-failed")
    assert handle._lease_phase == "state_db_unclean_integrity_check"
    assert check_state_db_integrity(home=tmp_path / "nowhere") == "absent"
