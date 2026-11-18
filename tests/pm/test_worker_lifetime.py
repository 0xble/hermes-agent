"""PM workers share caller custody and preserve completed replies at the deadline."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import psutil
import pytest

from tests.waiting import wait_until


def _gone(pid):
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.platforms("posix")
def test_caller_group_kill_reaches_worker_and_grandchild(tmp_path):
    pids = tmp_path / "pids.json"
    worker = (
        "import json, os, subprocess, sys, threading\n"
        "child = subprocess.Popen([sys.executable, '-I', '-c', "
        "'import threading; threading.Event().wait(3600)'])\n"
        f"open({str(pids)!r}, 'w').write(json.dumps([os.getpid(), child.pid]))\n"
        "threading.Event().wait(3600)\n"
    )
    caller = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
        "from pm.client_process import worker_process\n"
        f"with worker_process([sys.executable, '-I', '-c', {worker!r}], "
        "env=dict(os.environ), operation='sync_venv', timeout=60) as process:\n"
        "    process.wait()\n"
    )
    proc = subprocess.Popen([sys.executable, "-I", "-c", caller], start_new_session=True)
    descendants = []
    try:
        wait_until(lambda: pids.exists() and pids.read_text(), what="worker descendant handshake")
        descendants = json.loads(pids.read_text())
        os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX test, invocation-owned group
        proc.wait(timeout=5)
        wait_until(lambda: all(_gone(pid) for pid in descendants), what="caller group descendants to exit")
    finally:
        for pid in descendants:
            if not _gone(pid):
                psutil.Process(pid).kill()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_result_received_before_timer_cancel_survives_expiry(tmp_path, monkeypatch):
    from pm import client
    import pm.client_process as processes

    class ExpireOnCancel:
        def __init__(self, timeout, callback):
            self.callback = callback

        def start(self):
            pass

        def cancel(self):
            self.callback()

        def join(self):
            pass

    script = (
        "import json, sys\n"
        "request = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id': request['id'], 'type': 'result', 'result': 'completed'}), flush=True)\n"
    )
    monkeypatch.setattr(processes.threading, "Timer", ExpireOnCancel)
    monkeypatch.setattr(client, "_worker_command", lambda *args: [sys.executable, "-I", "-c", script])
    assert client._request("prune_cache", {"cache": str(tmp_path)}) == "completed"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("operation", ["sync_venv", "ensure"])
def test_control_eof_stops_sync_but_preserves_ensure_pause(tmp_path, operation):
    script = (
        "import json, os, queue, subprocess, sys, threading\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
        "from pm.worker import _read_controls\n"
        "child = subprocess.Popen([sys.executable, '-I', '-S', '-c', "
        "'import threading; threading.Event().wait(3600)'])\n"
        "print(json.dumps([os.getpid(), child.pid]), flush=True)\n"
        "pause = threading.Event()\n"
        "_read_controls(queue.Queue(), pause, os.dup(0), threading.Event())\n"
        "assert pause.is_set()\n"
        "child.kill(); child.wait(timeout=5)\n"
    )
    proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", script],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    descendants = []
    try:
        descendants = json.loads(proc.stdout.readline())
        proc.stdin.write(json.dumps({"id": "eof", "operation": operation}) + "\n")
        proc.stdin.close()
        code = proc.wait(timeout=5)
        assert code == (-signal.SIGKILL if operation == "sync_venv" else 0)
        wait_until(lambda: all(_gone(pid) for pid in descendants), what="EOF descendant cleanup")
    finally:
        for pid in descendants:
            if not _gone(pid):
                psutil.Process(pid).kill()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        proc.stdout.close()
