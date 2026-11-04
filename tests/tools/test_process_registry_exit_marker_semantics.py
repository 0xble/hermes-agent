"""Exit receipts do not opt ordinary jobs into restart-safe lifecycle semantics."""
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


def _spawned_checkpoint(tmp_path, monkeypatch, *, survivor=False):
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.process_registry.CHECKPOINT_PATH", checkpoint)
    # Exercise the actual wrapper/Popen/checkpoint path without requiring a user bus.
    monkeypatch.setattr("tools.process_registry.restart_safe_gateway_child_argv",
                        lambda argv, **kwargs: SimpleNamespace(argv=argv, mode="direct"))
    registry = ProcessRegistry()
    command = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(60)'"
    session = registry.spawn_local(command, cwd=str(tmp_path), survive_restart=survivor)
    assert session.pid is not None and session.process is not None and session._reader_thread is not None
    try:
        session.notify_on_complete = True
        registry._write_checkpoint()
        entry, = json.loads(checkpoint.read_text(encoding="utf-8-sig"))
        assert entry["exit_path"]
        assert entry["survive_restart"] is survivor
    finally:
        os.killpg(session.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only callers
        session.process.wait(timeout=10)
        session._reader_thread.join(timeout=10)
        assert not session._reader_thread.is_alive()
    # The old owner collected a real exit during cleanup; simulate a crash instead.
    Path(entry["exit_path"]).unlink(missing_ok=True)
    entry["systemd_unit"] = "hermes-worker-test.scope"
    return checkpoint, entry


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("survivor,fate,marker,scope_ok,late", [
    (*case, late)
    for case in [
        (False, "gone", None, True),
        (False, "gone", 37, True),
        (False, "gone", None, False),
        (False, "gone", 37, False),
        (False, "reused", None, True),
        (False, "reused", 37, True),
        (True, "gone", None, True),
        (True, "gone", 37, True),
        (True, "reused", None, True),
        (True, "reused", 37, True),
    ]
    for late in ([False] if case[0] else [False, True])
])
def test_new_checkpoint_keeps_lifecycle_opt_in(tmp_path, monkeypatch, survivor, fate, marker, scope_ok, late):
    checkpoint, entry = _spawned_checkpoint(tmp_path, monkeypatch, survivor=survivor)
    checkpoint.write_text(json.dumps([entry]), encoding="utf-8")
    successor = ProcessRegistry()
    stop_scope = Mock(return_value=scope_ok)
    monkeypatch.setattr("tools.process_registry._stop_systemd_unit", stop_scope)
    # Startup matrix includes opt-in controls; post-adoption matrix exercises the
    # ordinary file poller without granting it survivor identity/lifecycle rules.
    adopted = None
    poller = successor._survivor_poller_loop
    if late:
        monkeypatch.setattr(successor, "_detached_host_fate", lambda *args: "running")
        monkeypatch.setattr(successor, "_survivor_poller_loop", lambda session: None)
        assert successor.recover_from_checkpoint() == 1
        adopted = successor._running[entry["session_id"]]
        assert adopted._reader_thread is not None
        adopted._reader_thread.join(timeout=10)
        successor.stop_survivor_pollers()
        assert stop_scope.call_count == 0
    monkeypatch.setattr(successor, "_detached_host_fate", lambda *args: fate)
    if marker is not None:
        Path(entry["exit_path"]).write_text(f"{marker}\n", encoding="utf-8")
    if late:
        assert adopted is not None
        if not scope_ok:
            assert successor._poll_ordinary_detached(adopted)
            assert not adopted.exited
            assert json.loads(checkpoint.read_text(encoding="utf-8-sig")) == [entry]
            stop_scope.assert_called_once_with(entry["systemd_unit"])
            # The next pass must retry the scope before collecting the receipt.
            stop_scope.return_value = True
        poller(adopted)
        recovered = 1
    else:
        recovered = successor.recover_from_checkpoint()
    expected_stops = int(not survivor and fate == "gone")
    if late and not scope_ok:
        expected_stops += 1
    assert stop_scope.call_count == expected_stops, "an exit receipt must not bypass ordinary scope reaping"
    if expected_stops:
        assert all(call.args == (entry["systemd_unit"],) for call in stop_scope.call_args_list)
    discarded = fate == "reused" and (not survivor or marker is None)
    retained = expected_stops and not scope_ok and not late
    assert recovered == int(late or (not discarded and not retained))
    if (discarded and not late) or retained:
        assert entry["session_id"] not in successor._running
        assert entry["session_id"] not in successor._finished
        assert json.loads(checkpoint.read_text(encoding="utf-8-sig")) == ([entry] if retained else [])
        assert successor.completion_queue.empty()
        return
    if late and fate == "reused":
        assert adopted is not None
        assert adopted.exited and adopted.exit_code is None
        assert successor.completion_queue.empty()
        return
    session = successor.get(entry["session_id"])
    assert session is not None
    # Observe notification publication before invoking wait(), whose explicit
    # consumption may legitimately acknowledge an opt-in survivor first.
    assert session._completion_event.wait(10)
    event = successor.completion_queue.get(timeout=5)
    assert event["exit_code"] == marker
    assert successor.completion_queue.empty()
    result = successor.wait(entry["session_id"], timeout=10)
    assert result["exit_code"] == marker
    assert session.termination_source == ("" if marker is not None else
                                          "survivor_lost" if survivor else "checkpoint.exit_uncollected")


@pytest.mark.platforms("posix")
def test_ordinary_signalled_command_keeps_negative_wait_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.process_registry.CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = ProcessRegistry()
    # exec previously made this the waitable shell itself; the EXIT wrapper's
    # subshell must not change the public wait status to the shell's 128+signal.
    command = f"exec {shlex.quote(sys.executable)} -c 'import os, signal; os.kill(os.getpid(), signal.SIGKILL)'"  # windows-footgun: ok — POSIX-only
    session = registry.spawn_local(command, cwd=str(tmp_path))
    result = registry.wait(session.id, timeout=10)
    assert result["exit_code"] == -signal.SIGKILL  # windows-footgun: ok — POSIX-only
    assert Path(session.exit_path).read_text(encoding="utf-8-sig").strip() == str(-signal.SIGKILL)  # windows-footgun: ok — POSIX-only
