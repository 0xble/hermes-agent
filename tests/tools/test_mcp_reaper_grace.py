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
    for name, value in (("_stdio_pids", {}), ("_stdio_pgids", {}), ("_stdio_sessions", {}),
                        ("_stdio_processes", {}), ("_stdio_starttimes", {}),
                        ("_orphan_stdio_pids", set()), ("_orphan_stdio_pid_servers", {})):
        monkeypatch.setattr(lifecycle, name, value)
        if hasattr(transport, name):
            monkeypatch.setattr(transport, name, value)
    monkeypatch.setattr("tools.mcp_tool._update_death_supervisor", lambda *args: None)


@pytest.mark.parametrize("ledger", ["empty", "dead", "reused", "unverifiable", "exits_during_scan"])
def test_pre_teardown_refresh_cannot_recapture_an_unverified_wrapper(monkeypatch, ledger):
    """Refresh must extend the original incarnation, never recapture its numeric PID."""
    pid = 787878
    original = Mock(pid=pid)
    original.create_time.return_value = 100.0
    original.status.return_value = psutil.STATUS_RUNNING
    if ledger == "unverifiable":
        original.is_running.side_effect = psutil.AccessDenied(pid)
    elif ledger == "exits_during_scan":
        original.is_running.side_effect = [True, False]
    else:
        original.is_running.return_value = False
    child = Mock(pid=787879)
    child.create_time.return_value = 200.0
    original.children.return_value = [child]
    lifecycle._stdio_processes[pid] = {} if ledger == "empty" else {pid: original}
    process = Mock(side_effect=AssertionError("refresh recaptured a possibly reused PID"))
    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(lifecycle.os, "kill", Mock())
    monkeypatch.setattr(lifecycle.os, "killpg", Mock(), raising=False)

    lifecycle._remember_mcp_processes({pid}, refresh=True)

    assert child.pid not in lifecycle._stdio_processes[pid]
    process.assert_not_called()
    original.send_signal.assert_not_called()
    child.send_signal.assert_not_called()
    lifecycle.os.kill.assert_not_called()
    lifecycle.os.killpg.assert_not_called()


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


def test_recycled_pgid_with_unrelated_session_is_not_signalled(monkeypatch):
    """A numeric PGID reused by a new session is not MCP group authority."""
    pid = 727272
    original = Mock(pid=pid)
    original.is_running.return_value = False
    original.status.return_value = psutil.STATUS_ZOMBIE
    unrelated = Mock(pid=pid)
    unrelated.is_running.return_value = True
    unrelated.status.return_value = psutil.STATUS_RUNNING
    unrelated.create_time.return_value = 200.0
    original.send_signal.side_effect = AssertionError("recycled PGID was signalled")
    lifecycle._stdio_processes[pid] = {pid: original}
    lifecycle._stdio_pgids[pid] = pid
    lifecycle._stdio_sessions[pid] = 100.0
    lifecycle._orphan_stdio_pids.add(pid)
    monkeypatch.setattr(psutil, "process_iter", lambda: [unrelated])
    monkeypatch.setattr(lifecycle.os, "getpgid", lambda _: pid, raising=False)
    monkeypatch.setattr(lifecycle.os, "getsid", lambda _: 200, raising=False)
    monkeypatch.setattr(lifecycle.os, "killpg", Mock(), raising=False)
    calls = []
    monkeypatch.setattr(
        "tools.mcp_tool._update_death_supervisor",
        lambda verb, pgids: calls.append((verb, set(pgids))),
    )

    lifecycle._kill_orphaned_mcp_children()

    original.send_signal.assert_not_called()
    assert any(pid in pgids for verb, pgids in calls if verb == "unregister")


def test_recycled_setsid_group_with_recorded_leader_is_not_signalled(monkeypatch):
    """A recycled setsid leader cannot authorize its later surviving members."""
    pid = 747474
    original = Mock(pid=pid)
    original.is_running.return_value = False
    original.status.return_value = psutil.STATUS_ZOMBIE
    original.create_time.return_value = 100.0
    recycled_leader = Mock(pid=pid)
    recycled_leader.is_running.return_value = True
    recycled_leader.status.return_value = psutil.STATUS_RUNNING
    recycled_leader.create_time.return_value = 200.0
    survivor = Mock(pid=747475)
    survivor.is_running.return_value = True
    survivor.status.return_value = psutil.STATUS_RUNNING
    survivor.create_time.return_value = 300.0
    lifecycle._stdio_processes[pid] = {pid: original}
    lifecycle._stdio_pgids[pid] = pid
    lifecycle._stdio_sessions[pid] = pid  # start_new_session: sid == pgid == leader pid
    lifecycle._orphan_stdio_pids.add(pid)
    monkeypatch.setattr(
        psutil,
        "process_iter",
        Mock(side_effect=[[recycled_leader, survivor], [recycled_leader, survivor], [survivor]]),
    )
    monkeypatch.setattr(lifecycle.os, "getpgid", lambda _: pid, raising=False)
    monkeypatch.setattr(lifecycle.os, "getsid", lambda _: pid, raising=False)
    killpg = Mock(side_effect=AssertionError("recycled setsid group was signalled"))
    monkeypatch.setattr(lifecycle.os, "killpg", killpg, raising=False)

    # Release sees the recycled setsid leader and rejects its changed create-time;
    # after that leader exits, its same-session survivor must remain untouched.
    server = type("Server", (transport.MCPServerTransportMixin,), {"name": "test"})()
    transport.MCPServerTransportMixin._release_spawned_children(server, {pid})
    lifecycle._kill_orphaned_mcp_children()

    killpg.assert_not_called()


@pytest.mark.platforms("posix")
def test_exited_replacement_leader_cannot_authorize_unwitnessed_survivor(monkeypatch):
    """SID/PGID reuse remains unsafe after the replacement setsid leader exits."""
    pid = 757575
    original = Mock(pid=pid)
    original.is_running.return_value = False
    original.create_time.return_value = 100.0
    survivor = Mock(pid=757576)
    survivor.is_running.return_value = True
    survivor.status.return_value = psutil.STATUS_RUNNING
    survivor.create_time.return_value = 300.0
    lifecycle._stdio_pids[pid] = "test"
    lifecycle._stdio_processes[pid] = {pid: original}
    lifecycle._stdio_pgids[pid] = pid
    lifecycle._stdio_sessions[pid] = pid
    # Both leaders are gone: only the replacement's child remains, with the
    # same numeric SID/PGID but no connection to a recorded incarnation.
    monkeypatch.setattr(psutil, "process_iter", lambda: [survivor])
    monkeypatch.setattr(lifecycle.os, "getpgid", lambda _: pid)
    monkeypatch.setattr(lifecycle.os, "getsid", lambda _: pid)
    kill, killpg, wait = Mock(), Mock(), Mock(return_value=([], [survivor]))
    monkeypatch.setattr(lifecycle.os, "kill", kill)
    monkeypatch.setattr(lifecycle.os, "killpg", killpg)
    monkeypatch.setattr(psutil, "wait_procs", wait)
    calls = []
    monkeypatch.setattr(
        "tools.mcp_tool._update_death_supervisor",
        lambda verb, pgids: calls.append((verb, set(pgids))),
    )
    server = SimpleNamespace(name="test")

    transport.MCPServerTransportMixin._release_spawned_children(server, {pid})
    lifecycle._kill_orphaned_mcp_children()

    killpg.assert_not_called()
    kill.assert_not_called()
    survivor.send_signal.assert_not_called()
    original.send_signal.assert_not_called()
    wait.assert_not_called()
    assert not any(pid in pgids for verb, pgids in calls if verb == "unregister")


@pytest.mark.platforms("posix")
def test_active_group_without_spawn_witness_is_not_signalled(monkeypatch):
    """Failed process capture must not turn numeric SID/PGID into ownership."""
    pid = 767676
    survivor = Mock(pid=767677)
    survivor.is_running.return_value = True
    survivor.status.return_value = psutil.STATUS_RUNNING
    survivor.create_time.return_value = 300.0
    lifecycle._stdio_pids[pid] = "test"
    lifecycle._stdio_processes[pid] = {}
    lifecycle._stdio_pgids[pid] = pid
    lifecycle._stdio_sessions[pid] = pid
    monkeypatch.setattr(psutil, "process_iter", lambda: [survivor])
    monkeypatch.setattr(lifecycle.os, "getpgid", lambda _: pid)
    monkeypatch.setattr(lifecycle.os, "getsid", lambda _: pid)
    kill, killpg, wait = Mock(), Mock(), Mock(return_value=([], [survivor]))
    monkeypatch.setattr(lifecycle.os, "kill", kill)
    monkeypatch.setattr(lifecycle.os, "killpg", killpg)
    monkeypatch.setattr(psutil, "wait_procs", wait)
    calls = []
    monkeypatch.setattr(
        "tools.mcp_tool._update_death_supervisor",
        lambda verb, pgids: calls.append((verb, set(pgids))),
    )

    lifecycle._kill_orphaned_mcp_children(include_active=True)

    killpg.assert_not_called()
    kill.assert_not_called()
    survivor.send_signal.assert_not_called()
    wait.assert_not_called()
    assert not any(pid in pgids for verb, pgids in calls if verb == "unregister")


def test_unverifiable_live_group_is_not_unregistered(monkeypatch):
    """A live group whose member identity cannot be read stays supervised."""
    pid = 737373
    proc = Mock(pid=pid)
    proc.is_running.side_effect = psutil.AccessDenied(pid)
    lifecycle._stdio_processes[pid] = {pid: proc}
    lifecycle._stdio_pgids[pid] = pid
    lifecycle._stdio_sessions[pid] = pid
    lifecycle._orphan_stdio_pids.add(pid)
    monkeypatch.setattr(lifecycle.os, "getpgid", lambda _: pid, raising=False)
    monkeypatch.setattr(psutil, "process_iter", lambda: [proc])
    calls = []
    monkeypatch.setattr(
        "tools.mcp_tool._update_death_supervisor",
        lambda verb, pgids: calls.append((verb, set(pgids))),
    )

    lifecycle._kill_orphaned_mcp_children()

    assert not any(pid in pgids for verb, pgids in calls if verb == "unregister")


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
