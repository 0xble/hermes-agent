"""Silent or partial worker replies cannot hold the caller indefinitely."""
from __future__ import annotations

import json
import sys

import pytest

from pm.package import InstallError
from tests.waiting import process_alive


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

    for pid in json.loads(pids.read_text()):
        assert not process_alive(pid)
