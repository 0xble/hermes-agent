"""Real subprocess + SQLite regressions for the detached cron completion fence.

These run the actual scheduler.run_one_job -> run_job path with a no-agent
script. Only the output persistence boundary is delayed in the completion-race
case; neither scheduler entry point is replaced.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import sqlite3

import pytest

from cron import executions, jobs
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


# A process is necessary: the production watchdog uses os._exit and real PID
# fingerprints. Keep all durable state in the test profile, not the runner home.
_WORKER = r'''
import json, os, sys, time
from pathlib import Path
from cron import scheduler, executions, delivery_queue
from cron.scheduler_detached_worker import arm_hard_wall_timeout
from hermes_constants import set_hermes_home_override
home, job_file, mode, wall, marker = sys.argv[1:]
if mode == 'timeout-script-terminated':
    # Give the script runner time to observe the watchdog's SIGTERM before
    # os._exit. This exposes the pre-commit diagnostic-output window.
    from cron import scheduler_detached_worker
    original_terminate = scheduler_detached_worker._terminate_owned_descendants
    def terminate_then_yield(*args, **kwargs):
        result = original_terminate(*args, **kwargs)
        time.sleep(0.5)
        return result
    scheduler_detached_worker._terminate_owned_descendants = terminate_then_yield
from agent.monitoring import emitter
class RecordingEmitter:
    def emit(self, event):
        with Path(marker + '.events').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'status': event.status, 'delivery_outcome': event.delivery_outcome}) + '\n')
    def flush(self, **kwargs):
        pass
emitter.get_emitter = lambda: RecordingEmitter()
set_hermes_home_override(Path(home))
job = json.loads(Path(job_file).read_text())
assert executions.adopt_claimed_execution(job['execution_id'])
fence = arm_hard_wall_timeout(job['execution_id'], home, float(wall))
os.environ['_HERMES_CRON_EXTERNAL_WORKER'] = job['execution_id']
# Capture only the network transport: retain the real queue SQLite write.
def queue_delivery(job, content, **kwargs):
    if job.get('deliver') == 'origin':
        return None  # unresolved origin: no queue receipt
    delivery_queue.enqueue(job['execution_id'], job, content,
                           for_failure=kwargs.get('for_failure', False))
    job['last_delivery_queued'] = True
    return None
scheduler._deliver_result = queue_delivery
if mode == 'delivery-in-flight':
    def slow_delivery(job, content, **kwargs):
        Path(marker).write_text('terminal committed; queued delivery waiting')
        time.sleep(float(wall) * 3)
        return queue_delivery(job, content, **kwargs)
    scheduler._deliver_result = slow_delivery
if mode == 'crash':
    def crash(*args, **kwargs):
        raise RuntimeError('script crashed before completion')
    scheduler.run_job = crash
original_mark = scheduler.mark_job_run
def traced_mark(*args, **kwargs):
    Path(marker).write_text('mark entered')
    result = original_mark(*args, **kwargs)
    Path(marker).write_text('mark returned')
    return result
scheduler.mark_job_run = traced_mark
if mode == 'commit-in-flight':
    original_finish = scheduler.finish_execution
    def slow_finish(*args, **kwargs):
        if kwargs.get('require_running'):
            Path(marker).write_text('completion entered; commit not started')
            time.sleep(float(wall) * 2)
        return original_finish(*args, **kwargs)
    scheduler.finish_execution = slow_finish
if mode == 'commit-lost-before-delivery':
    original_finish = scheduler.finish_execution
    def raced_finish(*args, **kwargs):
        if kwargs.get('require_running'):
            original_finish(*args, **dict(kwargs, success=False, error='watchdog timeout'))
        return original_finish(*args, **kwargs)
    scheduler.finish_execution = raced_finish
if mode == 'completion-in-flight':
    original = scheduler.save_job_output
    def slow_save(*args, **kwargs):
        Path(marker).write_text('completion claimed; persistence entered')
        time.sleep(float(wall) * 3)
        return original(*args, **kwargs)
    scheduler.save_job_output = slow_save
result = scheduler.run_one_job(job, hard_wall_fence=fence)
if not result and 'hard wall-clock timeout' in str((executions.get_execution(job['execution_id']) or {}).get('error')):
    # The watchdog owns the exit code and descendant teardown; don't race it.
    time.sleep(5)
fence.set()
Path(marker).write_text('returned ' + str(result))
sys.exit(0 if result else 2)
'''


def _run(tmp_path, mode, *, wall=3.0, deliver="telegram:123"):
    home = tmp_path / "profile"
    home.mkdir()
    script = home / "scripts" / "job.py"
    script.parent.mkdir()
    marker = tmp_path / "worker.state"
    if mode in ("timeout", "timeout-script-terminated"):
        script.write_text("import time\ntime.sleep(30)\nprint('late success')\n")
    elif mode == "suppressed":
        script.write_text("print('[SILENT]')\n")
    else:
        script.write_text("print('script success')\n")
    token = set_hermes_home_override(home)
    try:
        job = jobs.create_job("script run", "every 1h", script=str(script), no_agent=True,
                              deliver=deliver)
        run = executions.create_execution(job["id"], source="builtin")
        assert executions.mark_execution_handoff_pending(run["id"])
    finally:
        reset_hermes_home_override(token)
    job["execution_id"] = run["id"]
    payload = tmp_path / "job.json"
    payload.write_text(json.dumps(job))
    env = {**os.environ, "HERMES_HOME": str(home)}
    proc = subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(home), str(payload), mode, str(wall), str(marker)],
        cwd=Path(__file__).resolve().parents[2], env=env, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        pytest.fail(f"detached worker did not terminate: {stdout}\n{stderr}")
    token = set_hermes_home_override(home)
    try:
        row = executions.get_execution(run["id"])
        queue_path = home / "cron" / "deliveries.db"
        queued = None
        if queue_path.exists():
            with sqlite3.connect(queue_path) as conn:
                result = conn.execute("SELECT status FROM deliveries WHERE execution_id=?", (run["id"],)).fetchone()
            queued = {"status": result[0]} if result else None
        stored = jobs.get_job(job["id"])
        outputs = list(jobs._job_output_dir(job["id"]).glob("*.md"))
        recovered = executions.recover_interrupted_executions()
    finally:
        reset_hermes_home_override(token)
    return proc.returncode, row, queued, stored, outputs, recovered, stdout, stderr, marker


@pytest.mark.platforms("macos")
def test_timeout_wins_before_run_job_completes_without_success_side_effects(tmp_path):
    code, row, queued, stored, outputs, recovered, out, err, _ = _run(tmp_path, "timeout")
    assert code == 124, (code, row, stored, queued, out, err)
    assert row["status"] == "failed" and "hard wall-clock timeout" in row["error"]
    assert queued is None
    assert outputs == [], [(str(p), p.read_text()) for p in outputs]
    assert stored["last_status"] not in ("ok", "delivery_queued")
    assert recovered == 0


@pytest.mark.platforms("macos")
def test_timeout_script_termination_diagnostic_does_not_publish_success(tmp_path):
    code, row, queued, stored, outputs, recovered, out, err, _ = _run(
        tmp_path, "timeout-script-terminated")
    assert code == 124, (code, row, stored, queued, out, err)
    assert row is not None and stored is not None
    assert row["status"] == "failed" and "hard wall-clock timeout" in row["error"]
    assert queued is None
    assert outputs == [], [(str(p), p.read_text()) for p in outputs]
    assert stored["last_status"] not in ("ok", "delivery_queued")
    assert recovered == 0


@pytest.mark.platforms("macos")
def test_completion_wins_and_persists_result_before_wall(tmp_path):
    code, row, queued, stored, outputs, recovered, out, err, marker = _run(tmp_path, "normal")
    assert code == 0, (code, row['status'], row['error'], stored.get('last_status'), queued, marker.read_text() if marker.exists() else None, out, err)
    assert row["status"] == "completed" and row["error"] is None, (row['error'], stored.get('last_status'), err)
    assert queued is not None and queued["status"] == "pending"
    assert len(outputs) == 1
    assert stored["last_status"] in ("ok", "delivery_queued")
    assert recovered == 0


@pytest.mark.platforms("macos")
def test_terminal_commit_survives_worker_death_before_delivery_call(tmp_path):
    code, row, queued, stored, outputs, recovered, out, err, marker = _run(
        tmp_path, "delivery-in-flight")
    assert code == 0, (code, row["status"], row["error"], err)
    assert row["status"] == "completed" and row["error"] is None
    assert queued is not None and queued["status"] == "pending"
    assert "terminal committed" in marker.read_text()
    token = set_hermes_home_override(tmp_path / "profile")
    try:
        from cron import delivery_queue
        sent = []
        assert delivery_queue.drain(lambda *args: sent.append(args)) == 1
        assert delivery_queue.drain(lambda *args: sent.append(args)) == 0
        assert len(sent) == 1
        assert executions.get_execution(row["id"])["delivery_status"] == "delivered"
    finally:
        reset_hermes_home_override(token)
    assert recovered == 0


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("deliver", ["telegram:123", "origin"])
def test_watchdog_commit_wins_before_delivery_and_does_not_mark_job_success(tmp_path, deliver):
    code, row, queued, stored, _, _, out, err, _ = _run(
        tmp_path, "commit-lost-before-delivery", wall=10, deliver=deliver)
    assert code == 2, (code, row, stored, out, err)
    assert row["status"] == "failed" and row["error"] == "watchdog timeout"
    assert stored["last_status"] not in ("ok", "delivery_queued")
    assert queued is None or queued["status"] == "pending"

@pytest.mark.platforms("macos")
def test_output_persistence_hang_before_commit_times_out(tmp_path):
    code, row, queued, stored, outputs, recovered, out, err, marker = _run(
        tmp_path, "completion-in-flight")
    assert code == 124, (code, row["status"], row["error"], err)
    assert row["status"] == "failed" and "hard wall-clock timeout" in row["error"]
    assert queued is None and outputs == []
    assert "persistence entered" in marker.read_text()
    assert recovered == 0


@pytest.mark.platforms("macos")
def test_completion_not_committed_at_cap_timeout_wins(tmp_path):
    code, row, queued, stored, outputs, recovered, out, err, marker = _run(
        tmp_path, "commit-in-flight")
    assert code == 124, (code, row["status"], row["error"], err)
    assert row["status"] == "failed" and "hard wall-clock timeout" in row["error"]
    assert queued is not None and queued["status"] == "pending" and outputs
    token = set_hermes_home_override(tmp_path / "profile")
    try:
        from cron import delivery_queue
        sent = []
        assert delivery_queue.drain(lambda *args: sent.append(args)) == 0
        assert sent == []
        assert delivery_queue.get_status(row["id"])["status"] == "suppressed"
    finally:
        reset_hermes_home_override(token)
    assert "commit not started" in marker.read_text()
    assert recovered == 0


@pytest.mark.platforms("macos")
def test_timeout_result_is_immutable_across_later_recovery(tmp_path):
    code, row, queued, stored, outputs, recovered, out, err, _ = _run(tmp_path, "timeout")
    assert code == 124, (code, row, stored, queued, out, err)
    token = set_hermes_home_override(tmp_path / "profile")
    try:
        assert executions.finish_execution(row["id"], success=True) is None
        assert executions.get_execution(row["id"])["finished_at"] == row["finished_at"]
        assert executions.recover_interrupted_executions() == 0
    finally:
        reset_hermes_home_override(token)
    assert queued is None and recovered == 0


@pytest.mark.platforms("macos")
def test_detached_classifies_queued_result_then_queue_projects_delivery(tmp_path):
    code, row, queued, _, _, _, out, err, marker = _run(tmp_path, "normal")
    assert code == 0, (out, err)
    assert row["delivery_outcome"] == "queued"
    assert (row["delivery_status"], row["delivery_status_provisional"]) == ("pending", 0)
    assert queued == {"status": "pending"}
    events = [json.loads(line) for line in Path(str(marker) + ".events").read_text().splitlines()]
    assert any(e == {"status": "completed", "delivery_outcome": "queued"} for e in events)
    token = set_hermes_home_override(tmp_path / "profile")
    try:
        assert executions.record_delivery_outcome(row["id"], "suppressed", resolve_provisional_status="suppressed") is None
        assert executions.get_execution(row["id"])["delivery_outcome"] == "queued"
        from cron import delivery_queue
        assert delivery_queue.drain(lambda *_: None) == 1
        assert executions.get_execution(row["id"])["delivery_status"] == "delivered"
    finally:
        reset_hermes_home_override(token)


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("mode, deliver, outcome", [
    ("suppressed", "telegram:123", "suppressed"),
    ("normal", "origin", "not_configured"),
])
def test_detached_non_enqueue_resolves_provisional_status(tmp_path, mode, deliver, outcome):
    code, row, queued, _, _, _, out, err, marker = _run(tmp_path, mode, deliver=deliver)
    assert code == 0, (out, err)
    assert queued is None
    assert row["delivery_outcome"] == outcome
    assert (row["delivery_status"], row["delivery_status_provisional"]) == ("suppressed", 0)
    events = [json.loads(line) for line in Path(str(marker) + ".events").read_text().splitlines()]
    assert any(e == {"status": "completed", "delivery_outcome": outcome} for e in events)


def test_in_gateway_completion_keeps_single_finish_write(tmp_path, monkeypatch):
    from cron import scheduler
    home = tmp_path / "gateway-profile"
    home.mkdir()
    script = home / "scripts" / "job.py"
    script.parent.mkdir()
    script.write_text("print('script success')\n", encoding="utf-8")
    token = set_hermes_home_override(home)
    try:
        job = jobs.create_job("gateway run", "every 1h", script=str(script), no_agent=True,
                              deliver="telegram:123")
        monkeypatch.setattr(scheduler, "_deliver_result", lambda *args, **kwargs: None)
        finish_calls = []
        original_finish = scheduler.finish_execution
        def tracked_finish(*args, **kwargs):
            finish_calls.append(kwargs)
            return original_finish(*args, **kwargs)
        monkeypatch.setattr(scheduler, "finish_execution", tracked_finish)
        def not_detached(*args, **kwargs):
            pytest.fail("in-gateway completion must use finish_execution only")
        monkeypatch.setattr(scheduler, "record_delivery_outcome", not_detached)
        assert scheduler.run_one_job(job) is True
        row = executions.latest_execution(job["id"])
        assert row is not None
        assert row["status"] == "completed" and row["delivery_outcome"] == "delivered", row["error"]
        assert row["delivery_status_provisional"] == 0
        assert len(finish_calls) == 1 and finish_calls[0]["delivery_outcome"] == "delivered"
    finally:
        reset_hermes_home_override(token)


@pytest.mark.platforms("macos")
def test_detached_crash_failure_classifies_after_early_result(tmp_path):
    code, row, queued, _, _, _, out, err, marker = _run(tmp_path, "crash")
    assert code == 2, (out, err)
    assert row["status"] == "failed" and "script crashed" in row["error"]
    assert row["delivery_outcome"] == "queued"
    assert queued == {"status": "pending"}
    events = [json.loads(line) for line in Path(str(marker) + ".events").read_text().splitlines()]
    assert any(e == {"status": "failed", "delivery_outcome": "queued"} for e in events)
