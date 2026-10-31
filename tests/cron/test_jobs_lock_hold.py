"""Completion reads must not turn the jobs lock into an execution-writer convoy."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading

import pytest

from cron import executions, jobs
from cron.occurrences import completed_occurrence


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", path)
    return path


def _completed_job():
    job = jobs.create_job(prompt="completion proof", schedule="every 4h")
    slot = (jobs._hermes_now() - timedelta(seconds=10)).isoformat()
    records = jobs.load_jobs()
    next(record for record in records if record["id"] == job["id"])["next_run_at"] = slot
    jobs.save_jobs(records)
    record = executions.create_execution(job["id"], source="builtin", scheduled_instant=slot)
    executions.finish_execution(record["id"], success=True)
    return job, slot


@pytest.mark.parametrize("operation", ["scan", "claim"])
def test_completion_read_does_not_wait_for_execution_writer(ledger, operation):
    job, slot = _completed_job()
    held, release = threading.Event(), threading.Event()

    def hold_ledger():
        with executions._transaction():
            held.set()
            assert release.wait(10), "test never released the execution transaction"

    holder = threading.Thread(target=hold_ledger)
    holder.start()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        assert held.wait(5)
        call = jobs.get_due_jobs if operation == "scan" else lambda: jobs.claim_job_for_fire(job["id"])
        result = pool.submit(call).result(timeout=2)
        assert result == ([] if operation == "scan" else False)
        # Still see committed completion proof while the unrelated writer is alive.
        assert completed_occurrence(job, slot)
        assert holder.is_alive()
    finally:
        release.set()
        holder.join(timeout=5)
        pool.shutdown(wait=True)
    assert not holder.is_alive()


def test_completion_query_is_read_only_and_sees_committed_wal_rows(ledger, monkeypatch):
    job, slot = _completed_job()
    writer = sqlite3.connect(ledger)
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE executions SET status='failed'")
        monkeypatch.setattr(sqlite3, "connect", traced)
        assert completed_occurrence(job, slot)
        assert statements
        assert all(sql.lstrip().upper().startswith("SELECT ") for sql in statements)
    finally:
        writer.rollback()
        writer.close()


def test_missing_completion_ledger_is_not_created(ledger):
    assert not completed_occurrence({"id": "missing"}, "2026-01-01T00:00:00Z")
    assert not ledger.exists()


def test_completion_read_configures_bounded_sqlite_busy_timeout(ledger, monkeypatch):
    job, slot = _completed_job()
    connect = sqlite3.connect
    timeouts = []

    class CompletionReader(sqlite3.Connection):
        def execute(self, query, *args, **kwargs):
            if query.startswith("SELECT id, finished_at"):
                timeouts.append(super().execute("PRAGMA busy_timeout").fetchone()[0])
            return super().execute(query, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=CompletionReader))
    assert completed_occurrence(job, slot)
    assert timeouts == [50]


def test_successful_reads_share_one_scan_deadline(ledger, monkeypatch):
    from types import SimpleNamespace
    from cron import occurrences

    job, slot = _completed_job()
    clock = iter([100.0, 100.01, 100.04, 100.06])
    monkeypatch.setattr(occurrences, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    connect = sqlite3.connect
    waits = []

    def reader(*args, **kwargs):
        waits.append(kwargs["timeout"])
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", reader)
    with occurrences.completion_read_scan():
        for _ in range(3):
            assert completed_occurrence(job, slot)
    assert waits == pytest.approx([0.04, 0.01, 0.0])


def test_busy_ledger_wait_is_shared_by_scan_and_resets_next_scan(ledger, monkeypatch):
    completed = [_completed_job() for _ in range(8)]
    writer = sqlite3.connect(ledger)
    connect = sqlite3.connect
    waits = []

    def reader(*args, **kwargs):
        waits.append(kwargs["timeout"])
        return connect(*args, **kwargs)

    try:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr(sqlite3, "connect", reader)
        # After the first busy read, the remaining jobs use unknown-proof fail-open.
        assert {j["id"] for j in jobs.get_due_jobs()} == {j["id"] for j, _ in completed}
        assert len(waits) == 1
        assert 0 <= waits[0] <= 0.05
        writer.rollback()
        # A subsequent scan has a fresh budget and can see every completion again.
        waits.clear()
        assert jobs.get_due_jobs() == []
        assert len(waits) == len(completed)
    finally:
        writer.rollback()
        writer.close()


def test_busy_completion_read_remains_unknown_after_bounded_wait(ledger, caplog):
    # DELETE-mode readers may be excluded by a writer. Unknown proof is still fail-open.
    with sqlite3.connect(ledger) as conn:
        conn.execute("CREATE TABLE executions (id, job_id, scheduled_instant, status, finished_at, claimed_at)")
    writer = sqlite3.connect(ledger)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        assert not completed_occurrence({"id": "job"}, "2026-01-01T00:00:00Z")
        assert "Cannot check completed occurrence" in caplog.text
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.platforms("posix")
def test_lock_timeout_names_real_owner_and_does_not_overwrite_it(tmp_path, monkeypatch, caplog):
    home = tmp_path / "holder-home"
    env = dict(os.environ, HERMES_HOME=str(home),
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    code = (
        "import sys; from cron import jobs; "
        "lock=jobs._jobs_lock(); lock.__enter__(); print('ready',flush=True); "
        "sys.stdin.readline(); lock.__exit__(None,None,None)"
    )
    child = subprocess.Popen([sys.executable, "-c", code], env=env, text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == "ready"
        with jobs.use_cron_store(home):
            path = jobs._jobs_lock_file()
            before = path.read_bytes()
            owner = json.loads(before.decode("utf-8-sig"))
            assert owner["pid"] == child.pid
            monkeypatch.setattr(jobs, "_JOBS_LOCK_TIMEOUT_SECONDS", 0)
            with caplog.at_level("WARNING", logger="cron.jobs"):
                with jobs._jobs_lock():
                    pass
            assert path.read_bytes() == before
            assert f"holder pid={child.pid}" in caplog.text
            assert "command=" in caplog.text and "operation=" in caplog.text
            assert re.search(r"held=\d+\.\d{3}s", caplog.text)
    finally:
        stdout, stderr = child.communicate("release\n", timeout=10)
    assert child.returncode == 0, stdout + stderr
    assert path.read_bytes() == b""


def test_nested_lock_owner_is_preserved_and_cleared_on_exception():
    with pytest.raises(RuntimeError, match="test exit"):
        with jobs._jobs_lock():
            path = jobs._jobs_lock_file()
            before = path.read_bytes()
            with jobs._jobs_lock():
                assert path.read_bytes() == before
            raise RuntimeError("test exit")
    assert path.read_bytes() == b""
    with jobs._jobs_lock():
        assert json.loads(path.read_text(encoding="utf-8-sig"))["pid"] == os.getpid()


def test_slow_lock_release_reports_holder_and_duration(monkeypatch, caplog):
    from cron import lock_owner

    write_owner = lock_owner.write_owner

    def older_owner(fd, operation):
        owner = write_owner(fd, operation)
        owner["started"] -= 6
        return owner

    monkeypatch.setattr(lock_owner, "write_owner", older_owner)
    with jobs._jobs_lock():
        pass
    assert "Cron jobs lock held for 6." in caplog.text
    assert f"pid={os.getpid()}" in caplog.text
    assert "command=" in caplog.text and "operation=" in caplog.text


def test_unavailable_owner_diagnostics_do_not_disable_lock(monkeypatch):
    from cron import lock_owner

    def unavailable(*_args):
        raise OSError("diagnostic write unavailable")

    monkeypatch.setattr(lock_owner, "write_owner", unavailable)
    with jobs._jobs_lock():
        assert jobs._jobs_lock_state.depth == 1
    assert jobs._jobs_lock_state.depth == 0
