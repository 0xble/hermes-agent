"""Shutdown must not walk the host process table before killing owned groups."""

import os
import signal
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from tools.environments import base
from tools.environments.local import LocalEnvironment
from tools.interrupt import set_interrupt

pytestmark = pytest.mark.platforms("linux", "macos")
RECORDED = 178864182760


@pytest.fixture
def owned_group(monkeypatch):
    proc = SimpleNamespace(
        pid=12345, _hermes_pgid=67890, _hermes_pgid_start=RECORDED,
        poll=lambda: 0, wait=lambda timeout=None: 0, kill=Mock(),
    )
    signals = []

    def killpg(pgid, sig):
        signals.append((pgid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(os, "getpgid", lambda _pid: 67890)
    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda _pid: RECORDED)
    return proc, signals


@pytest.mark.parametrize("trigger", ["gateway", "cleanup"])
@pytest.mark.parametrize("recycled", [False, True])
def test_shutdown_kill_does_not_wait_for_slow_children(monkeypatch, owned_group, trigger, recycled):
    """An indefinitely slow psutil scan cannot delay shutdown or authorize a recycled PGID."""
    proc, signals = owned_group
    env = object.__new__(LocalEnvironment)
    release = threading.Event()
    scanned = threading.Event()
    finished = threading.Event()
    errors = []
    owner_tid = threading.get_ident()
    if recycled:
        monkeypatch.setattr("gateway.status.get_process_start_time", lambda _pid: RECORDED + 500000)

    def slow_children(recursive=False):
        scanned.set()
        release.wait(10)
        return []

    monkeypatch.setattr(psutil, "Process", lambda _pid: SimpleNamespace(children=slow_children))
    if trigger == "cleanup":
        # Use the real cleanup admission, not a test-only shutdown switch.
        monkeypatch.setattr(base, "_live_foreground", {id(proc): (env, proc)})
        monkeypatch.setattr(base, "_spawns_in_flight", 0)

    def kill():
        try:
            if trigger == "gateway":
                set_interrupt(True, reason="gateway shutdown")
            if trigger == "cleanup":
                base.kill_live_foreground_processes()
            else:
                env._kill_process(proc)
        except BaseException as exc:
            errors.append(exc)
        finally:
            set_interrupt(False)
            finished.set()

    worker = threading.Thread(target=kill, daemon=True)
    try:
        worker.start()
        assert finished.wait(5), "shutdown blocked on the host-wide children() scan"
        assert not errors
        assert not scanned.is_set(), "shutdown must not enumerate the host process table"
        assert signals == ([] if recycled else [(67890, signal.SIGTERM), (67890, 0)])
    finally:
        release.set()
        worker.join(10)
        set_interrupt(False, owner_tid)


def test_execute_gateway_shutdown_skips_descendant_scan_on_deadline_worker(monkeypatch):
    """A real execute wait must inherit shutdown from its submitting tool thread."""
    env = LocalEnvironment()
    spawned = []
    ready = threading.Event()
    finished = threading.Event()
    release_scan = threading.Event()
    scan_threads = []
    errors = []
    results = []
    run_bash = env._run_bash

    def publish_spawn(*args, **kwargs):
        proc = run_bash(*args, **kwargs)
        spawned.append(proc)
        ready.set()
        return proc

    real_children = psutil.Process.children

    def slow_children(self, recursive=False):
        # The test live-system guard snapshots descendants inside its own killpg wrapper;
        # only a scan issued by the production kill path is under test.
        if not sys._getframe(1).f_code.co_filename.endswith(os.path.join("environments", "local.py")):
            return real_children(self, recursive=recursive)
        scan_threads.append(threading.current_thread().name)
        release_scan.wait(10)
        return []

    def execute():
        try:
            results.append(env.execute("sleep 20", timeout=20))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(env, "_run_bash", publish_spawn)
    monkeypatch.setattr(psutil.Process, "children", slow_children)
    worker = threading.Thread(target=execute, name="shutdown-tool", daemon=True)
    try:
        worker.start()
        assert ready.wait(5), "execute did not spawn its real subprocess"
        set_interrupt(True, worker.ident, reason="gateway shutdown")
        assert finished.wait(8), f"shutdown blocked in children() on {scan_threads}"
        assert not errors
        assert results[0]["returncode"] == 130
        assert not scan_threads, "shutdown must not enumerate the host process table"
        assert spawned[0].poll() is not None
    finally:
        release_scan.set()
        worker.join(10)
        set_interrupt(False, worker.ident)
        for proc in spawned:
            if proc.poll() is None:
                env._kill_process(proc)
        assert not worker.is_alive(), "execute worker survived cleanup"



def test_nested_deadline_execute_still_answers_stop_on_tool_thread():
    """An execute already acting for a tool thread (the pre-exec guard's remote read) keeps it.

    Rebinding the inner wait to its own deadline worker would hide /stop aimed at the tool thread.
    """
    from agent.deadline import run_bounded_sync
    from tools.interrupt import acting_for_tid

    env = LocalEnvironment()
    spawned = []
    ready = threading.Event()
    finished = threading.Event()
    results = []
    run_bash = env._run_bash

    def publish_spawn(*args, **kwargs):
        proc = run_bash(*args, **kwargs)
        spawned.append(proc)
        ready.set()
        return proc

    def tool_thread():
        token = acting_for_tid.set(threading.get_ident())
        try:
            results.append(run_bounded_sync(
                lambda: env.execute("sleep 20", timeout=20), 30, label="test.pre-exec-guard").value)
        finally:
            acting_for_tid.reset(token)
            finished.set()

    env._run_bash = publish_spawn
    worker = threading.Thread(target=tool_thread, name="stop-tool", daemon=True)
    try:
        worker.start()
        assert ready.wait(5), "execute did not spawn its real subprocess"
        set_interrupt(True, worker.ident, reason="explicit stop requested")
        assert finished.wait(10), "nested execute ignored /stop on its tool thread"
        assert results[0]["returncode"] == 130
    finally:
        set_interrupt(False, worker.ident)
        for proc in spawned:
            if proc.poll() is None:
                env._kill_process(proc)
        worker.join(10)

@pytest.mark.parametrize("recycled_after_term", [False, True])
def test_shutdown_escalation_rechecks_group_identity(monkeypatch, owned_group, recycled_after_term):
    proc, signals = owned_group
    children = Mock(side_effect=AssertionError("shutdown scanned descendants"))
    monkeypatch.setattr(psutil, "Process", lambda _pid: SimpleNamespace(children=children))
    monkeypatch.setattr("tools.environments.local._wait_for_group_exit", lambda *_: False)
    monkeypatch.setattr("gateway.status.get_process_start_time",
                        lambda _pid: RECORDED + (500000 if signals and recycled_after_term else 0))
    set_interrupt(True, reason="gateway shutdown")
    try:
        object.__new__(LocalEnvironment)._kill_process(proc)
    finally:
        set_interrupt(False)
    assert signals == ([(67890, signal.SIGTERM)] if recycled_after_term
                       else [(67890, signal.SIGTERM), (67890, signal.SIGKILL)])
    children.assert_not_called()


def test_shutdown_never_signals_the_callers_group(monkeypatch, owned_group):
    proc, signals = owned_group
    monkeypatch.setattr(os, "getpgrp", lambda: 67890)
    children = Mock(side_effect=AssertionError("shutdown scanned descendants"))
    monkeypatch.setattr(psutil, "Process", lambda _pid: SimpleNamespace(children=children))
    set_interrupt(True, reason="gateway shutdown")
    try:
        object.__new__(LocalEnvironment)._kill_process(proc)
    finally:
        set_interrupt(False)
    assert signals == []
    proc.kill.assert_called_once()
    children.assert_not_called()


@pytest.mark.parametrize("reused_descendant", [False, True])
def test_normal_kill_still_sweeps_identity_checked_escapees(monkeypatch, owned_group, reused_descendant):
    """Ordinary /stop still snapshots and identity-checks children outside the owned group."""
    proc, signals = owned_group
    descendant = Mock(pid=33333)
    monkeypatch.setattr(os, "getpgid", lambda pid: 99999 if pid == descendant.pid else 67890)
    descendant.is_running.return_value = not reused_descendant
    descendant.status.return_value = psutil.STATUS_RUNNING
    children = Mock(return_value=[descendant])
    monkeypatch.setattr(psutil, "Process", lambda _pid: SimpleNamespace(children=children))
    monkeypatch.setattr("tools.environments.local._wait_for_group_exit", lambda _proc, _pgid, _timeout: False)
    set_interrupt(True, reason="explicit stop requested")
    try:
        object.__new__(LocalEnvironment)._kill_process(proc)
    finally:
        set_interrupt(False)
    children.assert_called_once_with(recursive=True)
    assert (67890, signal.SIGTERM) in signals
    assert descendant.kill.call_count == (0 if reused_descendant else 1)
