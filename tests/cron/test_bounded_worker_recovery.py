"""Real SQLite and detached-process regression coverage for restart-safe cron runs."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from cron import delivery_queue, executions, scheduler
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override


def _home(path):
    path.mkdir(parents=True, exist_ok=True)
    return set_hermes_home_override(path)


def test_gateway_down_delivery_receipt_survives_restart_and_profile_switch(tmp_path, monkeypatch):
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    token = _home(home_a)
    try:
        run = executions.create_execution("brief", source="builtin")
        executions.mark_execution_handoff_pending(run["id"])
        # A separate detached worker finishes with no gateway or adapter process.
        worker = """import sys
from cron import executions, delivery_queue
run = sys.argv[1]
assert executions.adopt_claimed_execution(run) is not None
assert delivery_queue.enqueue(run, {'id': 'brief'}, 'finished')['status'] == 'pending'
assert executions.finish_execution(run, success=True, delivery_outcome='queued')['status'] == 'completed'
"""
        env = {**os.environ, "HERMES_HOME": str(home_a)}
        result = subprocess.run([sys.executable, "-c", worker, run["id"]], env=env,
                                cwd=Path(__file__).resolve().parents[2],
                                start_new_session=True, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        queued = delivery_queue.get_status(run["id"])
        assert queued["status"] == "pending"
        terminal = executions.get_execution(run["id"])
        assert terminal["delivery_status"] == "pending"
    finally:
        reset_hermes_home_override(token)

    other = _home(home_b)
    try:
        assert scheduler.drain_delivery_queue({}, None) == 0
    finally:
        reset_hermes_home_override(other)

    token = _home(home_a)
    try:
        sent = []
        def send(job, content, *, adapters, loop, for_failure):
            assert get_hermes_home().resolve() == home_a.resolve()
            assert adapters == {"owner": "a"}
            sent.append((job["id"], content))
            return None
        monkeypatch.setattr(scheduler, "_deliver_result", send)
        assert scheduler.drain_delivery_queue({"owner": "a"}, None) == 1
        assert sent == [("brief", "finished")]
        assert scheduler.drain_delivery_queue({"owner": "a"}, None) == 0
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


def test_finish_before_enqueue_projects_pending_then_terminal_delivery(tmp_path, monkeypatch):
    from cron import delivery_queue as queue, executions
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        monkeypatch.setattr(queue, "DELIVERY_DB", home / "cron" / "deliveries.db")
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")
        run = executions.create_execution("production-order", source="builtin")
        executions.mark_execution_handoff_pending(run["id"])
        assert executions.adopt_claimed_execution(run["id"])
        finished = executions.finish_execution(run["id"], success=True, output="result")
        assert finished["delivery_status"] == "unknown"
        assert finished["delivery_status_provisional"] == 1

        queued = queue.enqueue(run["id"], {"id": "production-order"}, "result")
        assert queued["status"] == "pending"
        assert executions.get_execution(run["id"])["delivery_status"] == "pending"

        assert queue.drain(lambda *_: None) == 1
        assert queue.get_status(run["id"])["status"] == "delivered"
        terminal = executions.get_execution(run["id"])
        assert terminal["delivery_status"] == "delivered"
        assert terminal["delivery_status_provisional"] == 0
    finally:
        reset_hermes_home_override(token)


def test_killed_between_finish_and_enqueue_remains_unknown_and_is_not_resent(tmp_path, monkeypatch):
    from cron import delivery_queue as queue, executions
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        monkeypatch.setattr(queue, "DELIVERY_DB", home / "cron" / "deliveries.db")
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")
        run = executions.create_execution("killed-gap", source="builtin")
        executions.mark_execution_handoff_pending(run["id"])
        assert executions.adopt_claimed_execution(run["id"])
        finished = executions.finish_execution(run["id"], success=True)
        assert finished["delivery_status"] == "unknown"
        assert finished["delivery_status_provisional"] == 1
        assert queue.drain(lambda *_: pytest.fail("must not resend absent queue receipt")) == 0
        assert executions.get_execution(run["id"])["delivery_status"] == "unknown"
    finally:
        reset_hermes_home_override(token)

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


def test_code_sha_uses_immutable_release_marker_before_git(tmp_path, monkeypatch):
    sha = "a" * 40
    release = tmp_path / "releases" / sha
    (release / "cron").mkdir(parents=True)
    (release / ".release-ready").write_text(sha + "\n", encoding="utf-8")
    monkeypatch.setattr(executions, "__file__", str(release / "cron" / "executions.py"))
    assert executions.running_code_sha() == sha


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


def test_external_worker_hard_wall_survives_abandoned_inactivity_future(tmp_path):
    """An inactivity timeout can return while its executor thread remains alive."""
    home = tmp_path / "profile"
    home.mkdir()
    token = _home(home)
    try:
        run = executions.create_execution("wedged", source="builtin")
        executions.mark_execution_handoff_pending(run["id"])
    finally:
        reset_hermes_home_override(token)
    payload = tmp_path / "payload.json"
    ack = tmp_path / "ack.json"
    payload.write_text(json.dumps({"job": {"id": "wedged", "execution_id": run["id"],
                                          "hard_wall_timeout_seconds": 3},
                                   "profile_home": str(home)}), encoding="utf-8")
    returned = tmp_path / "abandoned.json"
    code = """import concurrent.futures,json,sys,threading,time
from pathlib import Path
from types import SimpleNamespace
from cron import scheduler
import run_agent

# Keep the real run_one_job -> run_job -> executor/inactivity-watchdog path;
# replace only unrelated agent construction, policy and delivery dependencies.
class AbandonedAgent:
    def run_conversation(self, *args, **kwargs):
        time.sleep(60)
    def get_activity_summary(self):
        return {'seconds_since_activity': 10}
    def interrupt(self, *args):
        pass

run_agent.AIAgent = AbandonedAgent
scheduler._prepare_job_prompt = lambda *args: (None, 'test prompt')
scheduler._load_cron_job_config = lambda *args: SimpleNamespace(cfg={}, model='test')
scheduler._resolve_cron_agent_setup = lambda *args: scheduler._CronAgentSetup(model='test')
scheduler._open_cron_session_db = lambda *args: None
scheduler._construct_cron_agent = lambda *args, **kwargs: AbandonedAgent()
scheduler._FireAudit = lambda *args: SimpleNamespace(write=lambda *args: None)
scheduler._cron_inactivity_seconds = lambda: .1
original_idle_loop = scheduler._inactivity_watchdog_loop
scheduler._inactivity_watchdog_loop = lambda **kw: original_idle_loop(**{**kw, 'poll_s': .02})
original_wait = concurrent.futures.wait
concurrent.futures.wait = lambda fs, timeout=None, **kw: original_wait(fs, timeout=.02, **kw)
scheduler._save_compose_deliver = lambda *args, **kwargs: None
result = scheduler._run_external_worker_payload(Path(sys.argv[1]), Path(sys.argv[2]))
# Returning from the payload is not proof of process exit: the real executor's
# non-daemon thread is still running, and interpreter shutdown waits for it.
Path(sys.argv[3]).write_text(json.dumps({'result': result, 'worker_alive': any(
    t.is_alive() and not t.daemon and t is not threading.current_thread()
    for t in threading.enumerate())}), encoding='utf-8')
sys.exit(0 if result else 1)
"""
    env = {**os.environ, "HERMES_HOME": str(home)}
    process = subprocess.Popen([sys.executable, "-c", code, str(payload), str(ack), str(returned)],
                               env=env, cwd=Path(__file__).resolve().parents[2],
                               start_new_session=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == 1, (stdout, stderr)
        assert ack.exists(), (stdout, stderr)
        assert json.loads(returned.read_text(encoding="utf-8")) == {
            "result": True, "worker_alive": True}
        token = _home(home)
        try:
            row = executions.get_execution(run["id"])
            assert row is not None
            assert row["status"] == "failed"
            assert "idle for" in row["error"]
            assert "hard wall-clock" not in row["error"]
        finally:
            reset_hermes_home_override(token)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.platforms("macos")
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
