"""An isolated serve says so in the spawn ledger; an ordinary serve does not.

Desktop's attach-first discovery reads ``spawn-ledger.json`` and attaches to any live loopback
serve. ``hermes serve --isolated`` (the backend another machine's Desktop spawns over SSH) opts out
of the host singleton on the CLI side, so the ledger row must carry that fact as a structured field
for Desktop to honour it too. The ledger integration uses real processes against a temp
``HERMES_HOME``; helper regressions inject process-exit races.
"""

import json
import os
import socket
import subprocess
import sys
import time
from unittest.mock import Mock

import psutil
import pytest

from tests.waiting import kill_if_alive, process_alive, process_children, terminate_if_alive


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _await_ledger_row(ledger, proc, log, *, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        pids = {proc.pid, *(c.pid for c in process_children(proc.pid))}
        if not process_alive(proc.pid):
            break
        try:
            rows = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
        for row in rows:
            if row.get("purpose") == "serve" and row.get("pid") in pids and row.get("port"):
                return row
        time.sleep(0.1)
    pytest.fail(f"serve never registered (exit={proc.poll()}): {log.read_text(encoding='utf-8', errors='replace')}")


def _stop(proc):
    if proc.poll() is None:
        try:
            parent = psutil.Process(proc.pid)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            parent = None
        family = [*process_children(parent), parent] if parent is not None else []
        for p in family:
            terminate_if_alive(p)
        _, alive = psutil.wait_procs(family, timeout=5) if family else ([], [])
        for p in alive:
            kill_if_alive(p)
    proc.wait(timeout=10)


@pytest.mark.parametrize("parent_running", [False, True])
def test_await_ledger_row_reports_exit_when_parent_vanishes(tmp_path, monkeypatch, parent_running):
    proc = Mock(pid=123)
    proc.poll.side_effect = [None, 1]
    parent = Mock()
    parent.children.side_effect = psutil.NoSuchProcess(proc.pid if not parent_running else 124)
    parent.is_running.return_value = parent_running
    parent.status.return_value = psutil.STATUS_SLEEPING
    monkeypatch.setattr(psutil, "Process", Mock(return_value=parent))
    ledger = tmp_path / "spawn-ledger.json"
    row = {"purpose": "serve", "pid": proc.pid, "port": 1234}
    ledger.write_text(json.dumps([row]))
    log = tmp_path / "serve.log"
    log.write_text("serve exited during discovery")

    if parent_running:
        assert _await_ledger_row(ledger, proc, log) == row
    else:
        with pytest.raises(pytest.fail.Exception, match="serve never registered \\(exit=1\\): serve exited during discovery"):
            _await_ledger_row(ledger, proc, log)

    parent.children.assert_called_once_with(recursive=True)


@pytest.mark.parametrize("boundary", [
    "constructor", "children", "child_terminate", "parent_terminate", "child_kill", "parent_kill",
])
def test_stop_tolerates_process_reaped_at_psutil_boundaries(monkeypatch, boundary):
    proc = Mock(pid=123)
    proc.poll.return_value = None
    parent, child = Mock(pid=123), Mock(pid=124)
    for process in (parent, child):
        process.is_running.return_value = True
        process.status.return_value = psutil.STATUS_SLEEPING
    parent.children.return_value = [child]
    constructor = Mock(return_value=parent)
    target = {"constructor": constructor, "children": parent.children,
              "child_terminate": child.terminate, "parent_terminate": parent.terminate,
              "child_kill": child.kill, "parent_kill": parent.kill}[boundary]
    target.side_effect = psutil.NoSuchProcess(proc.pid)
    monkeypatch.setattr(psutil, "Process", constructor)
    wait_procs = Mock(side_effect=lambda family, timeout: ([], family))
    monkeypatch.setattr(psutil, "wait_procs", wait_procs)

    _stop(proc)

    proc.wait.assert_called_once_with(timeout=10)
    if boundary == "constructor":
        wait_procs.assert_not_called()
    else:
        family = [parent] if boundary == "children" else [child, parent]
        wait_procs.assert_called_once_with(family, timeout=5)
        parent.terminate.assert_called_once_with()
        parent.kill.assert_called_once_with()


def test_isolated_serve_ledger_row_is_marked_and_ordinary_serve_is_not(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "locks")}
    for key in ("HERMES_DESKTOP", "HERMES_PARENT_PID", "HERMES_PARENT_START_MARKER",
                "HERMES_DASHBOARD_SESSION_TOKEN", "HERMES_SPAWN"):
        env.pop(key, None)
    ledger = home / "spawn-ledger.json"

    # Ordinary first: started second it would attach to the isolated one and exit.
    launches = [("ordinary", []), ("isolated", ["--isolated"])]
    procs, rows = {}, {}
    try:
        for name, extra in launches:
            log = tmp_path / f"{name}.log"
            with log.open("w", encoding="utf-8") as out:
                procs[name] = subprocess.Popen(
                    [sys.executable, "-m", "hermes_cli.main", "serve", *extra,
                     "--host", "127.0.0.1", "--port", str(_free_port())],
                    env=env, stdout=out, stderr=subprocess.STDOUT,
                )
            rows[name] = _await_ledger_row(ledger, procs[name], log)
    finally:
        for proc in procs.values():
            _stop(proc)

    assert rows["isolated"].get("isolated") is True
    assert not rows["ordinary"].get("isolated")
