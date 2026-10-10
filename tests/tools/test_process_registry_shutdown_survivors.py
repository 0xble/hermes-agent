"""Bounded shutdown must not lose ownership or declare surviving workers killed."""

import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, suppress
from unittest.mock import patch

import psutil
import pytest

from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture
def registry():
    with patch("tools.process_registry._checkpoint_path", return_value=None), \
         patch("tools.process_registry.save_completed_result"), \
         patch.object(ProcessRegistry, "_write_checkpoint"):
        yield ProcessRegistry()


@contextmanager
def worker(script="print('ready', flush=True); import time; time.sleep(60)"):
    """Use a pipe acknowledgement, never startup sleeps, and always reap our child."""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    messages = []
    children = []
    ready = threading.Event()
    child_ready = threading.Event()

    def read_messages():
        for line in process.stdout:
            messages.append(line.strip())
            if line.startswith("ready"):
                ready.set()
            if line.startswith("child "):
                with suppress(psutil.NoSuchProcess):
                    # Cache the creation identity before the child can be reaped;
                    # psutil.kill refuses a later reuse of this announced PID.
                    children.append(psutil.Process(int(line.split()[1])))
                child_ready.set()

    reader = threading.Thread(target=read_messages, daemon=True)
    reader.start()
    try:
        assert ready.wait(5), "worker did not acknowledge startup"
        yield process, messages, child_ready
    finally:
        # If the test fails while shutdown is in flight, close every announced child
        # before its parent; only processes spawned by this fixture are eligible.
        for child in children:
            with suppress(psutil.NoSuchProcess):
                child.kill()
                child.wait(timeout=5)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        reader.join(timeout=5)
        process.stdout.close()
        process.stderr.close()


def tracked(registry, process, *, detached=False, sid="proc_shutdown_worker"):
    session = ProcessSession(
        id=sid, command="shutdown regression worker", task_id="shutdown-test",
        pid=process.pid, pid_scope="host", detached=detached,
        host_start_time=registry._safe_host_start_time(process.pid),
    )
    assert session.host_start_time is not None, "test requires a readable initial fingerprint"
    if not detached:
        session.process = process
    registry._running[session.id] = session
    return session


@pytest.mark.platforms("posix")
def test_unreadable_detached_identity_is_a_survivor(registry, monkeypatch):
    with worker() as (process, _messages, _child_ready):
        session = tracked(registry, process, detached=True)
        monkeypatch.setattr(ProcessRegistry, "_safe_host_start_time", staticmethod(lambda _pid: None))
        monkeypatch.setattr(registry, "_daemon_term_grace_seconds", lambda: 0.0)

        killed = registry.kill_all(deadline=time.monotonic() + 2.0)

        assert killed == 0, "unverifiable identity must not count as a successful kill"
        assert process.poll() is None
        assert registry._running[session.id] is session
        assert not session.exited
        assert session.completion_reason != "killed"
        assert registry.completion_queue.empty()


@pytest.mark.platforms("posix")
def test_pending_scope_stop_cannot_starve_active_term(registry, monkeypatch):
    """Main's gone-scope cleanup is the pre-TERM retry path (no finished retry flag)."""
    with worker() as (process, _messages, _child_ready), \
         worker() as (gone, _gone_messages, _gone_child_ready):
        pending = tracked(registry, gone, detached=True, sid="proc_pending_scope")
        pending.systemd_unit = "hermes-worker-test-pending.scope"
        gone.terminate()
        gone.wait(timeout=5)
        tracked(registry, process)
        term_sent = threading.Event()
        stop_entered = threading.Event()
        release_stop = threading.Event()
        killpg = os.killpg

        def observe_signal(pgid, sig):
            if pgid == process.pid and sig == signal.SIGTERM:
                term_sent.set()
            return killpg(pgid, sig)

        def spend_budget(_unit, *, timeout=None):
            assert timeout is not None
            stop_entered.set()
            release_stop.wait(timeout=max(0.0, deadline - time.monotonic()))
            return False

        monkeypatch.setattr("tools.process_registry.os.killpg", observe_signal)
        monkeypatch.setattr("tools.process_registry._stop_systemd_unit", spend_budget)
        deadline = time.monotonic() + 2.0
        try:
            registry.kill_all(deadline=deadline)
            assert stop_entered.is_set(), "scope cleanup was not attempted"
            assert term_sent.is_set(), "pending scope exhausted the deadline before active TERM"
            process.wait(timeout=5)
        finally:
            release_stop.set()


# The child can be reparented to init before fixture cleanup; it is still ours.
@pytest.mark.live_system_guard_bypass
@pytest.mark.platforms("posix")
def test_escalation_refreshes_descendants_born_after_term(registry, monkeypatch):
    script = """
import signal, subprocess, sys, time

def on_term(_sig, _frame):
    child = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(60)'],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print('child', child.pid, flush=True)

signal.signal(signal.SIGTERM, on_term)
print('ready', flush=True)
time.sleep(60)
"""
    with worker(script) as (process, messages, child_ready):
        session = tracked(registry, process)
        monkeypatch.setattr(registry, "_daemon_term_grace_seconds", lambda: 2.0)
        finished = threading.Event()
        result = {}

        def shutdown():
            try:
                result["killed"] = registry.kill_all(deadline=time.monotonic() + 8.0)
            finally:
                finished.set()

        sweep = threading.Thread(target=shutdown)
        sweep.start()
        try:
            assert child_ready.wait(5), "TERM handler did not acknowledge its detached child"
            child_pid = int(next(line for line in messages if line.startswith("child ")).split()[1])
            assert finished.wait(10), "bounded sweep did not finish"
            sweep.join(timeout=5)
            process.wait(timeout=5)
            assert not registry._is_host_pid_alive(child_pid), "post-TERM detached child survived escalation"
            assert result["killed"] == 1
            assert session.completion_reason == "killed"
        finally:
            if sweep.is_alive():
                process.kill()
                sweep.join(timeout=10)
