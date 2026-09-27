"""Real SQLite and detached-process regression coverage for restart-safe cron runs."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from cron import delivery_queue, executions
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _home(path):
    path.mkdir(parents=True, exist_ok=True)
    return set_hermes_home_override(path)


def test_gateway_down_delivery_receipt_survives_restart_and_profile_switch(tmp_path):
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    token = _home(home_a)
    try:
        run = executions.create_execution("brief", source="builtin")
        executions.mark_execution_running(run["id"])
        # The detached worker can persist the delivery without gateway adapters.
        queued = delivery_queue.enqueue(run["id"], {"id": "brief"}, "finished")
        assert queued["status"] == "pending"
        terminal = executions.finish_execution(run["id"], success=True, delivery_outcome="queued")
        assert terminal["delivery_status"] == "pending"
    finally:
        reset_hermes_home_override(token)

    other = _home(home_b)
    try:
        assert delivery_queue.drain(lambda *_: pytest.fail("wrong profile")) == 0
    finally:
        reset_hermes_home_override(other)

    token = _home(home_a)
    try:
        sent = []
        assert delivery_queue.drain(lambda job, content, failure: sent.append((job["id"], content)) or None) == 1
        assert sent == [("brief", "finished")]
        assert delivery_queue.drain(lambda *_: pytest.fail("duplicate send")) == 0
        after = executions.get_execution(run["id"])
        assert after["status"] == "completed"
        assert after["delivery_status"] == "delivered"
        assert after["finished_at"] == terminal["finished_at"]
        assert after["code_sha"] == executions.running_code_sha()
        assert after["execution_identity"] == run["id"]
        assert executions.finish_execution(run["id"], success=False, error="late") is None
        assert executions.get_execution(run["id"])["status"] == "completed"
    finally:
        reset_hermes_home_override(token)


def test_live_detached_owner_reconciles_without_rewriting_even_if_stale(monkeypatch, tmp_path):
    token = _home(tmp_path / "home")
    try:
        run = executions.create_execution("worker", source="builtin")
        executions.mark_execution_handoff_pending(run["id"])
        adopted = executions.adopt_claimed_execution(run["id"])
        assert adopted["owner_kind"] == "detached"
        assert adopted["process_started_at"] is not None
        monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-gateway")
        monkeypatch.setattr(executions, "_claim_age_seconds", lambda _: 999999)
        assert executions.recover_interrupted_executions() == 0
        assert executions.get_execution(run["id"])["status"] == "running"
    finally:
        reset_hermes_home_override(token)


def test_pid_reuse_and_unreadable_fingerprint_fail_closed(monkeypatch, tmp_path):
    import gateway.status
    token = _home(tmp_path / "home")
    try:
        run = executions.create_execution("worker", source="builtin")
        executions.mark_execution_handoff_pending(run["id"])
        executions.adopt_claimed_execution(run["id"])
        monkeypatch.setattr(executions, "_PROCESS_ID", "replacement")
        monkeypatch.setattr(gateway.status, "_pid_exists", lambda _: True)
        prior = executions.get_execution(run["id"])["process_started_at"]
        monkeypatch.setattr(executions, "_process_start_time", lambda _: prior + 10000)
        assert executions.recover_interrupted_executions() == 0
        assert executions.get_execution(run["id"])["status"] == "running"
        monkeypatch.setattr(executions, "_process_start_time", lambda _: None)
        assert executions.recover_interrupted_executions() == 0
        assert executions.get_execution(run["id"])["status"] == "running"
    finally:
        reset_hermes_home_override(token)


def test_old_schema_reader_can_insert_and_select_after_additive_migration(tmp_path, monkeypatch):
    path = tmp_path / "cron" / "executions.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE executions (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
            process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
            status TEXT NOT NULL CHECK(status IN ('claimed','running','completed','failed','unknown')),
            handoff_pending INTEGER NOT NULL DEFAULT 0, handoff_started_at REAL,
            claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT,
            delivery_outcome TEXT, scheduled_instant TEXT)""")
        db.execute("INSERT INTO executions (id,job_id,source,process_id,pid,status,claimed_at) "
                   "VALUES ('old','job','cron','old',1,'completed','2026-01-01')")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", path)
    fresh = executions.create_execution("new", source="builtin")
    with sqlite3.connect(path) as old_reader:
        assert old_reader.execute("SELECT id,status FROM executions WHERE id='old'").fetchone() == ("old", "completed")
        old_reader.execute("INSERT INTO executions (id,job_id,source,process_id,pid,status,claimed_at) "
                           "VALUES ('rollback','job','cron','old',1,'claimed','2026-01-02')")
        assert old_reader.execute("SELECT id,status FROM executions WHERE id=?", (fresh["id"],)).fetchone() == (fresh["id"], "claimed")


@pytest.mark.macos_only
def test_hard_wall_watchdog_kills_setsid_grandchild_once(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    pidfile = tmp_path / "grandchild.pid"
    code = """import os,subprocess,sys,time
from pathlib import Path
from cron import executions
from cron.scheduler_detached_worker import arm_hard_wall_timeout
run = executions.create_execution('timeout', source='builtin')
executions.mark_execution_running(run['id'])
Path(sys.argv[2]).write_text(run['id'])
child = subprocess.Popen([sys.executable, '-c', 'import os,sys,time; os.setsid(); open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(60)', sys.argv[1]])
while not Path(sys.argv[1]).exists(): time.sleep(.01)
arm_hard_wall_timeout(run['id'], Path(os.environ['HERMES_HOME']), .4)
time.sleep(60)
"""
    env = {**os.environ, "HERMES_HOME": str(home)}
    idfile = tmp_path / "execution.id"
    process = subprocess.Popen([sys.executable, "-c", code, str(pidfile), str(idfile)], env=env,
                               cwd=Path(__file__).resolve().parents[2], start_new_session=True)
    try:
        assert process.wait(timeout=15) == 124
        execution_id = idfile.read_text()
        grandchild_pid = int(pidfile.read_text())
        import psutil
        assert not psutil.pid_exists(grandchild_pid) or psutil.Process(grandchild_pid).status() == psutil.STATUS_ZOMBIE
        token = _home(home)
        try:
            row = executions.get_execution(execution_id)
            assert row["status"] == "failed" and "hard wall-clock timeout" in row["error"]
            assert executions.recover_interrupted_executions() == 0
            assert executions.get_execution(execution_id)["finished_at"] == row["finished_at"]
            assert executions.finish_execution(execution_id, success=True) is None
        finally:
            reset_hermes_home_override(token)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if pidfile.exists():
            import psutil
            with __import__('contextlib').suppress(psutil.NoSuchProcess):
                psutil.Process(int(pidfile.read_text())).kill()
