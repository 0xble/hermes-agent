"""Missing receipts preserve base PID/scope safety; survival remains opt-in."""
import json
import os
from pathlib import Path
import shlex
import signal
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.process_registry import ProcessRegistry


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["ordinary", "persistent", "survivor"])
@pytest.mark.parametrize("fate,scope_ok", [("gone", True), ("gone", False), ("reused", True)])
def test_unrecorded_checkpoint_preserves_pid_and_scope_safety(tmp_path, monkeypatch, kind, fate, scope_ok):
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.process_registry.CHECKPOINT_PATH", checkpoint)
    monkeypatch.setattr("tools.process_registry.restart_safe_gateway_child_argv",
                        lambda argv, **kwargs: SimpleNamespace(argv=argv, mode="direct"))
    producer = ProcessRegistry()
    command = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(60)'"
    session = producer.spawn_local(command, cwd=str(tmp_path),
                                   persist_on_release=kind == "persistent", survive_restart=kind == "survivor")
    try:
        session.notify_on_complete = True
        producer._write_checkpoint()
        entry, = json.loads(checkpoint.read_text(encoding="utf-8-sig"))
        assert bool(entry.get("exit_path")) == (kind != "ordinary")
    finally:
        os.killpg(session.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only test
        session.process.wait(timeout=10)
        session._reader_thread.join(timeout=10)
        assert not session._reader_thread.is_alive()
    if entry.get("exit_path"):
        Path(entry["exit_path"]).unlink(missing_ok=True)
    entry["systemd_unit"] = "hermes-worker-test.scope"
    checkpoint.write_text(json.dumps([entry]), encoding="utf-8")
    successor = ProcessRegistry()
    monkeypatch.setattr(successor, "_detached_host_fate", lambda *args: fate)
    stop_scope = Mock(return_value=scope_ok)
    monkeypatch.setattr("tools.process_registry._stop_systemd_unit", stop_scope)
    recovered = successor.recover_from_checkpoint()
    expected_stops = int(kind != "survivor" and fate == "gone")
    assert stop_scope.call_count == expected_stops
    if expected_stops:
        stop_scope.assert_called_once_with(entry["systemd_unit"])
    retained = bool(expected_stops and not scope_ok)
    discarded = fate == "reused"
    assert recovered == int(not retained and not discarded)
    if retained or discarded:
        assert entry["session_id"] not in successor._running
        assert entry["session_id"] not in successor._finished
        assert json.loads(checkpoint.read_text(encoding="utf-8-sig")) == ([entry] if retained else [])
        assert successor.completion_queue.empty()
    else:
        adopted = successor.get(session.id)
        assert adopted is not None and adopted._completion_event.wait(10)
        assert adopted.exited and adopted.exit_code is None
        assert adopted.termination_source == ("survivor_lost" if kind == "survivor" else "checkpoint.exit_uncollected")
        event = successor.completion_queue.get_nowait()
        assert event["exit_code"] is None
        assert successor.completion_queue.empty()


@pytest.mark.platforms("posix")
def test_ordinary_signalled_command_keeps_negative_wait_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.process_registry.CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = ProcessRegistry()
    command = f"exec {shlex.quote(sys.executable)} -c 'import os, signal; os.kill(os.getpid(), signal.SIGKILL)'"  # windows-footgun: ok — POSIX-only
    session = registry.spawn_local(command, cwd=str(tmp_path))
    result = registry.wait(session.id, timeout=10)
    assert result["exit_code"] == -signal.SIGKILL  # windows-footgun: ok — POSIX-only
    assert not session.exit_path
