"""Real macOS escape from the cron worker process session."""

import os
import subprocess
import sys
import time
import uuid
import threading

import psutil
import pytest


@pytest.mark.platforms("macos")
@pytest.mark.live_system_guard_bypass
def test_hard_wall_sweeps_double_forked_orphan(tmp_path):
    from cron.executions import _process_start_time
    from cron.scheduler_detached_worker import _terminate_owned_descendants

    execution_id = uuid.uuid4().hex
    pid_file = tmp_path / "orphan.pid"
    script = (
        "import os,time,sys\n"
        "if os.fork(): sys.exit(0)\n"
        "os.setsid()\n"
        "if os.fork(): os._exit(0)\n"
        "with open(sys.argv[1], 'w') as f: f.write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    env = dict(os.environ, _HERMES_CRON_EXTERNAL_WORKER=execution_id)
    subprocess.run([sys.executable, "-c", script, str(pid_file)],
                   env=env, check=True, timeout=5)
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists()
    pid = int(pid_file.read_text())
    try:
        assert psutil.Process(pid).ppid() != os.getpid()
        assert _terminate_owned_descendants(
            os.getpid(), _process_start_time(os.getpid()), execution_id)
        deadline = time.monotonic() + 5
        while psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    finally:
        if psutil.pid_exists(pid):
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.platforms("macos")
@pytest.mark.live_system_guard_bypass
def test_script_timeout_spares_unrelated_worker_child(monkeypatch, tmp_path):
    from cron import scheduler as sched
    from cron.scheduler_script import _run_job_script

    execution_id = uuid.uuid4().hex
    monkeypatch.setenv("_HERMES_CRON_EXTERNAL_WORKER", execution_id)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sched, "_SCRIPT_TIMEOUT", 1)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    orphan_pid_file = tmp_path / "orphan.pid"
    script = scripts / "fork.py"
    script.write_text(
        "import os,time,sys\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    if os.fork(): os._exit(0)\n"
        f"    with open({str(orphan_pid_file)!r}, 'w') as f: f.write(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "else:\n"
        "    time.sleep(60)\n"
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        ok, output = _run_job_script(str(script))
        assert not ok and "timed out" in output
        assert orphan_pid_file.exists()
        orphan = int(orphan_pid_file.read_text())
        deadline = time.monotonic() + 5
        while psutil.pid_exists(orphan) and psutil.Process(orphan).status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not psutil.pid_exists(orphan) or psutil.Process(orphan).status() == psutil.STATUS_ZOMBIE
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)
        if orphan_pid_file.exists():
            orphan = int(orphan_pid_file.read_text())
            if psutil.pid_exists(orphan):
                try:
                    os.kill(orphan, 9)
                except ProcessLookupError:
                    pass

@pytest.mark.platforms("macos")
@pytest.mark.live_system_guard_bypass
def test_script_cancel_sweeps_reparented_child(monkeypatch, tmp_path):
    from cron.scheduler_script import _run_job_script

    execution_id = uuid.uuid4().hex
    monkeypatch.setenv("_HERMES_CRON_EXTERNAL_WORKER", execution_id)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    pid_file = tmp_path / "orphan.pid"
    script = scripts / "fork.py"
    script.write_text(
        "import os,time,sys\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    if os.fork(): os._exit(0)\n"
        f"    with open({str(pid_file)!r}, 'w') as f: f.write(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "else:\n"
        "    time.sleep(60)\n"
    )
    cancelled = threading.Event()

    def cancel_after_orphan():
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        cancelled.set()

    trigger = threading.Thread(target=cancel_after_orphan, daemon=True)
    trigger.start()
    try:
        ok, output = _run_job_script(str(script), cancel_event=cancelled)
        assert not ok and "cancelled" in output
        assert pid_file.exists()
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while (psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
               and time.monotonic() < deadline):
            time.sleep(0.02)
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    finally:
        trigger.join(timeout=2)
        if pid_file.exists():
            pid = int(pid_file.read_text())
            if psutil.pid_exists(pid):
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
