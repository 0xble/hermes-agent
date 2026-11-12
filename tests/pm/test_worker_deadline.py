"""Silent or partial worker replies cannot hold the caller indefinitely."""
from __future__ import annotations

import json
import sys

import pytest

from pm.package import InstallError
from tests.waiting import kill_if_alive, process_alive, wait_until


@pytest.mark.parametrize("partial_reply", [False, True])
def test_worker_deadline_reaps_the_worker_and_its_child(tmp_path, monkeypatch, partial_reply):
    from pm import client
    import pm.client_process as processes

    pids = tmp_path / "pids.json"
    script = (
        "import json, os, subprocess, sys, threading\n"
        "sys.stdin.readline()\n"
        "child = subprocess.Popen([sys.executable, '-I', '-c', "
        "'import threading; threading.Event().wait(3600)'])\n"
        f"open({str(pids)!r}, 'w').write(json.dumps([os.getpid(), child.pid]))\n"
        + ("sys.stdout.write('{\"id\":'); sys.stdout.flush()\n" if partial_reply else "")
        + "threading.Event().wait(3600)\n"
    )
    monkeypatch.setattr(processes, "WORKER_TIMEOUT", 2)
    monkeypatch.setattr("pm.worker_budget.operation_budget", lambda *args: 2)
    monkeypatch.setattr(client, "_worker_command", lambda *args: [sys.executable, "-I", "-c", script])

    with pytest.raises(InstallError, match="worker prune_cache timed out after 2s"):
        client._request("prune_cache", {"cache": str(tmp_path)})

    worker, child = json.loads(pids.read_text())
    try:
        assert not process_alive(worker)
        # The caller reaps its worker; the non-child may still be completing SIGKILL exit.
        wait_until(lambda: not process_alive(child), timeout=5, what="worker child to exit")
    finally:
        kill_if_alive(child)
