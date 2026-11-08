"""Loaded clients survive source updates without losing negotiated deadlines."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from tests.pm._fixtures import (
    client as client,  # noqa: PLC0414 -- pytest fixture discovery
    isolated_python as isolated_python,  # noqa: PLC0414 -- pytest fixture discovery
    worker_toolchain,
)


@pytest.mark.parametrize("accepts_deadline", [False, True])
def test_worker_reports_only_negotiated_deadlines(client, monkeypatch, isolated_python, accepts_deadline):
    import pm.client_process as processes

    worker_toolchain(client, monkeypatch, isolated_python, injection=(
        "from pm.worker_budget import unbounded\n"
        "import pm.build_operations\n"
        "def prune_cache(**kwargs):\n"
        "    with unbounded():\n"
        "        kwargs['progress']('working')\n"
        "    return 'completed'\n"
        "pm.build_operations.prune_cache = prune_cache\n"
    ))
    progress = []
    if accepts_deadline:
        suspensions = []
        original = processes.WorkerDeadline.suspend

        def suspend(self):
            suspensions.append(self.remaining)
            original(self)

        monkeypatch.setattr(processes.WorkerDeadline, "suspend", suspend)
        assert client._request("prune_cache", {}, callbacks={"progress": progress.append}) == "completed"
        assert len(suspensions) == 1
    else:
        from pm.runtime import runtime_environment

        worker = Path(client.__file__).with_name("worker.py")
        request = {"id": "legacy", "operation": "prune_cache", "arguments": {},
                   "callbacks": ["progress"], "packages": [],
                   "context": {"repo": str(worker.parent.parent), "lockfile": "unused"}}
        with subprocess.Popen(client.runtime_command(worker), env=runtime_environment(),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as process:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            # The pre-deadline client treats every non-result message as a callback.
            while True:
                response = json.loads(process.stdout.readline())
                if response["type"] == "result":
                    assert response["result"] == "completed"
                    break
                assert response["callback"] == "progress"
                progress.append(*response["args"])
                process.stdin.write(json.dumps({"id": request["id"], "call": response["call"],
                                                "result": None}) + "\n")
                process.stdin.flush()
            process.stdin.close()
            assert process.wait(timeout=10) == 0
    assert progress == ["working"]


def test_sync_result_eof_exits_normally_and_releases_lease(client, monkeypatch, isolated_python, tmp_path):
    from hermes_cli.runtime_state import leases_held

    generation = tmp_path / "generation"
    generation.mkdir()
    (generation / ".lease-managed").touch()
    lease_seen = tmp_path / "lease-seen"
    worker_toolchain(client, monkeypatch, isolated_python, injection=(
        "import pm.runtime, pm.install, pm.worker\n"
        "from hermes_cli.runtime_state import lease_directory, leases_held\n"
        f"generation = Path({str(generation)!r})\n"
        "pm.runtime.lease_current_runtime = lambda: lease_directory(generation)\n"
        "def sync_venv(**kwargs):\n"
        "    assert leases_held(generation)\n"
        f"    Path({str(lease_seen)!r}).touch()\n"
        "    return 'synced'\n"
        "pm.install.sync_venv = sync_venv\n"
        "read_controls = pm.worker._read_controls\n"
        "eof = __import__('threading').Event()\n"
        "def controls(*args):\n"
        "    read_controls(*args)\n"
        "    eof.set()\n"
        "pm.worker._read_controls = controls\n"
        # Keep main alive until stdin EOF is processed, making the shutdown race deterministic.
        "def run():\n"
        "    pm.worker.main()\n"
        "    assert eof.wait(10), 'client did not close stdin'\n"
        "run()\n"
        "raise SystemExit(0)\n"
    ))
    exits = []
    original_wait = subprocess.Popen.wait

    def wait(process, *args, **kwargs):
        code = original_wait(process, *args, **kwargs)
        exits.append(code)
        return code

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    assert client._request("sync_venv", {"plugins": {"kind": "members", "paths": []}}) == "synced"
    assert lease_seen.exists()
    assert exits and set(exits) == {0}
    assert not leases_held(generation)
    assert not list((generation / ".leases").iterdir())
