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
set_hermes_home_override(Path(home))
job = json.loads(Path(job_file).read_text())
assert executions.adopt_claimed_execution(job['execution_id'])
fence = arm_hard_wall_timeout(job['execution_id'], home, float(wall))
os.environ['_HERMES_CRON_EXTERNAL_WORKER'] = job['execution_id']
# Capture only the network transport: retain the real queue SQLite write.
def queue_delivery(job, content, **kwargs):
    delivery_queue.enqueue(job['execution_id'], job, content,
                           for_failure=kwargs.get('for_failure', False))
    return None
scheduler._deliver_result = queue_delivery
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
if mode == 'completion-in-flight':
    original = scheduler.save_job_output
    def slow_save(*args, **kwargs):
        Path(marker).write_text('completion claimed; persistence entered')
        time.sleep(float(wall) * 3)
        return original(*args, **kwargs)
    scheduler.save_job_output = slow_save
result = scheduler.run_one_job(job, hard_wall_fence=fence)
fence.set()
Path(marker).write_text('returned ' + str(result))
sys.exit(0 if result else 2)
'''


def _run(tmp_path, mode, *, wall=3.0):
    home = tmp_path / "profile"
    home.mkdir()
    script = home / "scripts" / "job.py"
    script.parent.mkdir()
    marker = tmp_path / "worker.state"
    if mode == "timeout":
        script.write_text("import time\ntime.sleep(30)\nprint('late success')\n")
    else:
        script.write_text("print('script success')\n")
    token = set_hermes_home_override(home)
    try:
        job = jobs.create_job("script run", "every 1h", script=str(script), no_agent=True,
                              deliver="telegram:123")
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


@pytest.mark.macos_only
def test_timeout_wins_before_run_job_completes_without_success_side_effects(tmp_path):
    code, row, queued, stored, outputs, recovered, out, err, _ = _run(tmp_path, "timeout")
    assert code == 124, (code, row, stored, queued, out, err)
    assert row["status"] == "failed" and "hard wall-clock timeout" in row["error"]
    assert queued is None
    assert outputs == []
    assert stored["last_status"] not in ("ok", "delivery_queued")
    assert recovered == 0


@pytest.mark.macos_only
def test_completion_wins_and_persists_result_before_wall(tmp_path):
    code, row, queued, stored, outputs, recovered, out, err, marker = _run(tmp_path, "normal")
    assert code == 0, (code, row['status'], row['error'], stored.get('last_status'), queued, marker.read_text() if marker.exists() else None, out, err)
    assert row["status"] == "completed" and row["error"] is None, (row['error'], stored.get('last_status'), err)
    assert queued is not None and queued["status"] == "pending"
    assert len(outputs) == 1
    assert stored["last_status"] in ("ok", "delivery_queued")
    assert recovered == 0


@pytest.mark.macos_only
def test_completion_commits_then_teardown_hangs_past_cap_plus_grace(tmp_path):
    code, row, queued, stored, outputs, recovered, out, err, marker = _run(
        tmp_path, "completion-in-flight")
    assert code == 0, (code, row["status"], row["error"], err)
    assert row["status"] == "completed" and row["error"] is None
    assert queued is None and outputs == []
    assert row["delivery_status"] == "unknown"  # commit-to-enqueue gap, never replay
    assert "persistence entered" in marker.read_text()
    token = set_hermes_home_override(tmp_path / "profile")
    try:
        from cron import delivery_queue
        sent = []
        assert delivery_queue.drain(lambda *args: sent.append(args)) == 0
        assert delivery_queue.drain(lambda *args: sent.append(args)) == 0
        assert sent == []
        assert executions.get_execution(row["id"])["delivery_status"] == "unknown"
    finally:
        reset_hermes_home_override(token)
    assert recovered == 0


@pytest.mark.macos_only
def test_completion_not_committed_at_cap_timeout_wins(tmp_path):
    code, row, queued, stored, outputs, recovered, out, err, marker = _run(
        tmp_path, "commit-in-flight")
    assert code == 124, (code, row["status"], row["error"], err)
    assert row["status"] == "failed" and "hard wall-clock timeout" in row["error"]
    assert queued is None and outputs == []
    assert "commit not started" in marker.read_text()
    assert recovered == 0


@pytest.mark.macos_only
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
