"""Detached local results survive the process that launched the registry."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession
from tools.process_registry_notifications import format_process_notification


def test_ordinary_local_exit_survives_spawner(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    checkpoint = home / "processes.json"
    monkeypatch.setattr("tools.process_registry.CHECKPOINT_PATH", checkpoint)
    script = tmp_path / "spawn.py"
    script.write_text("""
import os
from tools.process_registry import ProcessRegistry
registry = ProcessRegistry()
session = registry.spawn_local("sleep 1; printf 'durable-tail\\n'; exit 37")
session.notify_on_complete = True
registry._write_checkpoint()
os._exit(0)
""")
    spawned = subprocess.run([sys.executable, str(script)], env={**os.environ, "PYTHONPATH": str(Path.cwd())},
                             capture_output=True, text=True, timeout=15, check=False)
    assert spawned.returncode == 0, spawned.stderr
    entry, = json.loads(checkpoint.read_text())
    assert entry["exit_path"], "ordinary jobs must record an exit outside their spawner"
    deadline = time.monotonic() + 15
    while not Path(entry["exit_path"]).exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert Path(entry["exit_path"]).read_text().strip() == "37"
    successor = ProcessRegistry()
    assert successor.recover_from_checkpoint() == 1
    result = successor.wait(entry["session_id"], timeout=10)
    assert result["exit_code"] == 37
    assert "durable-tail" in result["output"]
    assert successor.completion_queue.get(timeout=5)["exit_code"] == 37


def test_killed_wrapper_persists_its_wait_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry = ProcessRegistry()
    session = registry.spawn_local("sleep 30", cwd=str(tmp_path))
    try:
        assert session.pid is not None
        os.killpg(session.pid, signal.SIGKILL)
        result = registry.wait(session.id, timeout=10)
        assert result["exit_code"] == -signal.SIGKILL
        assert Path(session.exit_path).read_text().strip() == str(-signal.SIGKILL)
    finally:
        if not session.exited:
            registry.kill_process(session.id)


@pytest.mark.parametrize("recover", [False, True])
def test_legacy_gone_exit_is_retained_and_announced(tmp_path, monkeypatch, recover):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr("tools.process_registry.CHECKPOINT_PATH", checkpoint)
    child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(19)"])
    child.wait(timeout=10)
    log = tmp_path / "legacy.log"
    log.write_text("legacy-tail\n")
    session = ProcessSession(id="proc_legacy_exit", command="legacy", pid=child.pid,
                             detached=True, log_path=str(log), notify_on_complete=True)
    registry = ProcessRegistry()
    registry._running[session.id] = session
    if recover:
        registry._write_checkpoint()
        registry = ProcessRegistry()
        assert registry.recover_from_checkpoint() == 1
    result = registry.poll(session.id)
    assert result["status"] == "exited"
    assert result["exit_code"] is None
    event = registry.completion_queue.get_nowait()
    assert "legacy-tail" in event["output"]
    assert "exited, status unknown (not collected)" in (format_process_notification(event) or "")
    registry.poll(session.id)
    assert registry.completion_queue.empty()
