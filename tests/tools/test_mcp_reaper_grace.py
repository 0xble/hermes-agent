"""MCP teardown waits for owned live incarnations, not a nonempty PID ledger."""

import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from tools import mcp_tool_lifecycle as lifecycle
from tools import mcp_tool_transport as transport


@pytest.fixture(autouse=True)
def isolated_ledger(monkeypatch):
    for name, value in (("_stdio_pids", {}), ("_stdio_pgids", {}),
                        ("_orphan_stdio_pids", set()), ("_orphan_stdio_pid_servers", {})):
        monkeypatch.setattr(lifecycle, name, value)
        if hasattr(transport, name):
            monkeypatch.setattr(transport, name, value)
    monkeypatch.setattr(lifecycle, "_stdio_processes", {}, raising=False)
    monkeypatch.setattr("tools.mcp_tool._update_death_supervisor", lambda *args: None)


@pytest.mark.parametrize("ledger", ["empty", "dead", "reused", "unverifiable"])
def test_reaper_skips_wait_and_signals_without_a_live_owned_incarnation(monkeypatch, ledger):
    pid = 999999999
    if ledger in ("dead", "unverifiable"):
        original = Mock(pid=pid)
        if ledger == "dead":
            original.is_running.return_value = False
        else:
            original.is_running.side_effect = psutil.AccessDenied(pid)
        lifecycle._stdio_processes[pid] = {pid: original}
    if ledger == "reused":
        # Capture a spawn incarnation, then make its handle report PID reuse.
        original = Mock(pid=pid)
        original.children.return_value = []
        original.is_running.return_value = True
        original.status.return_value = psutil.STATUS_RUNNING
        original.create_time.return_value = 100.0
        monkeypatch.setattr(psutil, "Process", lambda _: original)
        monkeypatch.setattr(lifecycle.os, "getpgid", lambda _: os.getpgrp(), raising=False)
        monkeypatch.setattr("hermes_cli.process_identity.register_child", lambda *args: None)
        server = SimpleNamespace(name="test")
        transport.MCPServerTransportMixin._track_spawned_children(server, {pid})
        # A handle whose (pid, create-time) no longer matches must be discarded.
        original.is_running.return_value = False
        original.send_signal.side_effect = AssertionError("reused PID was signalled")
    if ledger != "empty":
        lifecycle._orphan_stdio_pids.add(pid)
    sleep = Mock(side_effect=AssertionError("dead or unverified PIDs must not consume grace"))
    monkeypatch.setattr(lifecycle.time, "sleep", sleep)
    wait = Mock(side_effect=AssertionError("no owned survivor needs a wait"))
    monkeypatch.setattr(psutil, "wait_procs", wait)
    kill = Mock()
    monkeypatch.setattr(lifecycle.os, "kill", kill)
    monkeypatch.setattr(lifecycle.os, "killpg", kill, raising=False)
    lifecycle._kill_orphaned_mcp_children()
    sleep.assert_not_called()
    wait.assert_not_called()
    kill.assert_not_called()
    if ledger != "empty":
        original.send_signal.assert_not_called()


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("ignore_term", [False, True])
def test_reaper_preserves_two_second_grace_only_for_live_owned_survivors(monkeypatch, ignore_term):
    handler = "signal.SIG_IGN" if ignore_term else "signal.SIG_DFL"
    child = subprocess.Popen(
        [sys.executable, "-c", f"import signal,sys; signal.signal(signal.SIGTERM, {handler}); "
         "print('ready', flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        # Child announces readiness only after installing its signal disposition.
        assert child.stdout.readline().strip() == "ready"
        server = SimpleNamespace(name="test")
        transport.MCPServerTransportMixin._track_spawned_children(server, {child.pid})
        transport.MCPServerTransportMixin._release_spawned_children(server, {child.pid})
        real_wait = psutil.wait_procs
        waits, exit_codes = [], []

        def wait(procs, timeout=None, **kwargs):
            waits.append(timeout)
            gone, alive = real_wait(procs, timeout=timeout, **kwargs)
            exit_codes.extend(proc.returncode for proc in gone)
            return gone, alive

        monkeypatch.setattr(psutil, "wait_procs", wait)
        started = time.monotonic()
        lifecycle._kill_orphaned_mcp_children()
        elapsed = time.monotonic() - started
        child.wait(timeout=10)
        assert waits, "reaper must wait on verified processes rather than blind sleep"
        assert waits[0] == pytest.approx(2.0, abs=0.05)
        assert elapsed < 10
        if ignore_term:
            assert elapsed >= 1.9, "SIGKILL must not shorten the existing two-second grace"
            assert child.returncode == -signal.SIGKILL
        else:
            assert -signal.SIGTERM in exit_codes  # psutil.wait_procs already reaped the child
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("leader_already_exited", [False, True])
def test_reaper_waits_for_owned_grandchild_after_group_leader_exits(leader_already_exited):
    script = """
import os, signal, sys
r, w = os.pipe()
child = os.fork()
if child == 0:
    os.close(r)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(w, b'ready')
    os.close(w)
    signal.pause()
else:
    os.close(w)
    os.read(r, 5)
    os.close(r)
    print(child, flush=True)
    sys.stdin.read()
"""
    parent = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, start_new_session=True)
    grandchild = None
    try:
        grandchild = psutil.Process(int(parent.stdout.readline()))
        grandchild.create_time()
        server = SimpleNamespace(name="test")
        transport.MCPServerTransportMixin._track_spawned_children(server, {parent.pid})
        if leader_already_exited:
            parent.terminate()
            parent.wait(timeout=10)
        transport.MCPServerTransportMixin._release_spawned_children(server, {parent.pid})
        assert parent.pid in lifecycle._orphan_stdio_pids
        lifecycle._kill_orphaned_mcp_children()
        parent.wait(timeout=10)
        grandchild.wait(timeout=10)
        assert not lifecycle._mcp_process_alive(grandchild)
    finally:
        if grandchild is not None and grandchild.is_running():
            grandchild.kill()
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=10)
        parent.stdin.close()
        parent.stdout.close()
