"""An unclean gateway death must trigger a state.db integrity check.

Regression for the 2026-08-31 incident. ``state.db`` was corrupt from
2026-08-26 evening (a SIGKILL landed on a gateway mid-WAL-checkpoint during a
``--replace`` restart storm), but nothing checked the file. The damage sat in
old, rarely-read session rows for 3.5 days until a Desktop read tripped over
it on 2026-08-30 17:15 and surfaced as "Session not found".

``record_startup`` already detects the unclean exit and logs "SIGKILL / OOM /
VM death" — it just never looked at the database that death may have torn.
The check is gated on the unclean exit and runs off the startup path: a
large healthy store must not delay readiness, and a clean boot must not scan.
"""
from __future__ import annotations

import json
import logging
import threading
import sqlite3
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


def _make_state_db(home: Path, *, corrupt: bool) -> Path:
    """Build a real SQLite file, optionally with a genuinely torn b-tree page."""
    path = home / "state.db"
    conn = sqlite3.connect(path)
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
    """Join real checker threads before assertions or fixture teardown."""
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

    monkeypatch.setattr(provider, "spawn_context_thread", track)
    yield wait
    wait()


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


def test_unclean_exit_records_the_corruption_verdict(tmp_path: Path, integrity_workers, caplog) -> None:
    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path)

    assert evidence is not None
    integrity_workers()
    record = _exit_diag_records(tmp_path)[0]
    assert record["state_db_integrity"] not in ("ok", "absent")
    assert any(r.levelno == logging.ERROR and "FAILED integrity check" in r.message for r in caplog.records)


def test_unclean_exit_on_a_healthy_store_records_ok(tmp_path: Path, integrity_workers) -> None:
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path)

    assert evidence is not None
    integrity_workers()
    assert _exit_diag_records(tmp_path)[0]["state_db_integrity"] == "ok"


def test_clean_exit_does_not_pay_for_the_check(tmp_path: Path, monkeypatch) -> None:
    """A clean boot must not scan the store — that is the whole cost gate."""
    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path, phase="exited")

    called = []
    import gateway.lifecycle_ledger as ledger

    monkeypatch.setattr(
        ledger, "check_state_db_integrity", lambda **kw: called.append(1) or "ok"
    )
    record_startup(home=tmp_path)

    assert not called, "integrity check ran on a clean boot"


@pytest.mark.parametrize("verdict", ["ok", "btree page is corrupt"])
def test_startup_claims_and_returns_while_integrity_check_is_running(
    tmp_path: Path, monkeypatch, integrity_workers, caplog, verdict
) -> None:
    """A held check cannot hold up readiness, its verdict, or the unclean warning."""
    import gateway.lifecycle_ledger as ledger
    import hermes_startup_watchdog as sw
    from agent.memory_provider import spawn_context_thread

    handle = sw.StartupWatchdogHandle(timeout_s=300.0, exit_code=75)
    monkeypatch.setattr(sw, "_handle", handle)
    _write_sentinel(tmp_path)
    started, release, returned = threading.Event(), threading.Event(), threading.Event()
    result = []

    def slow_check(*, home):
        assert home == tmp_path
        started.set()
        assert release.wait(timeout=5), "test did not release the checker"
        return verdict

    def boot():
        result.append(record_startup(home=tmp_path))
        returned.set()

    monkeypatch.setattr(ledger, "check_state_db_integrity", slow_check)
    # Keep a regression on the old synchronous implementation bounded too.
    caller = spawn_context_thread(boot, name="test-startup-caller")
    caller.start()
    try:
        assert started.wait(timeout=2), "integrity check was not started"
        assert returned.wait(timeout=2), "startup waited on the integrity check"
        assert not release.is_set()
        sentinel = json.loads(get_lifecycle_sentinel_path(tmp_path).read_text())
        assert sentinel["phase"] == "running" and sentinel["prior_unclean_exit"] is True
        assert result[0]["prior_pid"] == _DEAD_PID
        assert "state_db_integrity" not in result[0]
        assert _exit_diag_records(tmp_path) == []
        assert "exited UNCLEANLY" in caplog.text
        assert handle._lease_count == 0, "background work masked startup liveness"
    finally:
        release.set()
        caller.join(timeout=5)
        integrity_workers()

    record = _exit_diag_records(tmp_path)[0]
    assert record["tag"] == "gateway.previous_unclean_exit"
    assert record["prior_pid"] == _DEAD_PID
    assert record["state_db_integrity"] == verdict
    errors = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("FAILED integrity check" in msg for msg in errors) == (verdict != "ok")
    assert handle._lease_count == 0
