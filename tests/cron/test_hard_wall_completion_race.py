"""Real worker/descendant contention at the detached cron hard-wall boundary."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cron import delivery_queue, executions
from hermes_constants import reset_hermes_home_override, set_hermes_home_override



@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGTERM handling")
@pytest.mark.parametrize("completion_first", [False, True], ids=["timeout-wins", "completion-wins"])
def test_hard_wall_vs_completion_with_slow_descendant_cleanup(tmp_path, completion_first):
    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        run = executions.create_execution("race", source="builtin")
        executions.mark_execution_handoff_pending(run["id"])
    finally:
        reset_hermes_home_override(token)

    # One child exits promptly; the other ignores SIGTERM for a second. On the
    # old implementation this window let completion persist success, then the
    # watchdog exited 124 after cleanup and failed to rewrite the terminal row.
    worker = '''import os, subprocess, sys, threading, time
from cron import delivery_queue, executions
from cron.scheduler_detached_worker import arm_hard_wall_timeout
run, mode, marker = sys.argv[1:]
assert executions.adopt_claimed_execution(run)
fast = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.4)'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
slow = subprocess.Popen([sys.executable, '-c',
    'import signal,sys,time; '
    'marker=sys.argv[1]; '
    'signal.signal(signal.SIGTERM, lambda *_: (open(marker, "w").write("signaled"), time.sleep(5))); '
    'open(marker, "w").write("ready"); time.sleep(30)', marker],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(200):
    if os.path.exists(marker): break
    if slow.poll() is not None: raise RuntimeError(f'slow exited {slow.returncode}')
    time.sleep(.005)
else: raise RuntimeError(f'slow stuck {slow.pid}')
fence = arm_hard_wall_timeout(run, os.environ['HERMES_HOME'], 1.0)
def complete():
    if mode == 'completion':
        time.sleep(.5)  # win shortly before the 1s wall, with scheduling headroom
    else:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if open(marker).read() == 'signaled': break
            time.sleep(.005)
        else: raise RuntimeError('watchdog did not begin descendant cleanup')
    if executions.finish_execution(run, success=True, require_running=True) is not None:
        delivery_queue.enqueue(run, {'id': 'race'}, 'SUCCESS')
thread = threading.Thread(target=complete)
thread.start()
thread.join(timeout=4)
if mode == 'timeout':
    time.sleep(4)  # watchdog must terminate us before this point
    sys.exit(9)
time.sleep(12)  # completed ledger retains its result, watchdog must bound process
sys.exit(9)
'''
    marker = tmp_path / "slow-ready"
    env = {**os.environ, "HERMES_HOME": str(home)}
    process = subprocess.Popen(
        [sys.executable, "-c", worker, run["id"],
         "completion" if completion_first else "timeout", str(marker)],
        env=env, cwd=Path(__file__).resolve().parents[2],
        start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        try:
            stdout, stderr = process.communicate(timeout=12)
        except subprocess.TimeoutExpired as exc:
            pytest.fail(f"worker stalled: {exc.stdout!r} {exc.stderr!r}")
        assert process.returncode == (0 if completion_first else 124), (stdout, stderr)
        token = set_hermes_home_override(home)
        try:
            row = executions.get_execution(run["id"])
            assert row["status"] == ("completed" if completion_first else "failed"), row
            if completion_first:
                assert row["error"] is None
                assert delivery_queue.get_status(run["id"])["status"] == "pending"
            else:
                assert "hard wall-clock timeout" in row["error"]
                assert delivery_queue.get_status(run["id"]) is None
            assert executions.recover_interrupted_executions() == 0
        finally:
            reset_hermes_home_override(token)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
