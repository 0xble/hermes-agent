"""Only outliving jobs need receipts; attached jobs retain native Popen semantics."""
import json
from pathlib import Path
import time
import sys
import shutil
from unittest.mock import Mock

import psutil
import pytest

from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture
def local_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.process_registry.CHECKPOINT_PATH", tmp_path / "processes.json")
    monkeypatch.setattr("tools.process_registry.worker_output_log", lambda _: None)
    # Use the platform's supported shell (macOS's bash 3.2 stays interactive).
    shell = shutil.which("zsh" if sys.platform == "darwin" else "bash")
    assert shell is not None
    monkeypatch.setattr("tools.process_registry._find_shell", lambda: shell)
    return ProcessRegistry()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("code", [129, 130, 137, 192])
def test_attached_explicit_high_exit_is_not_a_signal(local_registry, tmp_path, code):
    session = local_registry.spawn_local(f"exit {code}", cwd=str(tmp_path))
    result = local_registry.wait(session.id, timeout=10)
    assert result["exit_code"] == code
    assert session.process.returncode == code
    assert not session.exit_path
    assert not (tmp_path / "process-runs").exists()


def _sleep_started(parent):
    for process in [parent, *parent.children(recursive=True)]:
        try:
            if process.name() == "sleep":
                return True
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            # A login-shell child can be reaped after the snapshot.
            continue
    return False


@pytest.mark.parametrize("error", [psutil.NoSuchProcess, psutil.ZombieProcess])
def test_sleep_probe_tolerates_vanished_child(error):
    parent, vanished, sleeper = (Mock(spec=psutil.Process) for _ in range(3))
    parent.name.return_value = "shell"
    vanished.name.side_effect = error(123)
    sleeper.name.return_value = "sleep"
    parent.children.return_value = [vanished]
    assert not _sleep_started(parent)
    parent.children.return_value = [vanished, sleeper]
    assert _sleep_started(parent)


@pytest.mark.platforms("posix")
def test_attached_sleep_retains_fast_native_kill(local_registry, tmp_path):
    session = local_registry.spawn_local("sleep 30", cwd=str(tmp_path))
    try:
        entry, = json.loads((tmp_path / "processes.json").read_text(encoding="utf-8-sig"))
        # Wait for shell startup, not for completion: kill an actually running sleep.
        parent = psutil.Process(session.pid)
        ready_deadline = time.monotonic() + 5
        while not _sleep_started(parent):
            assert time.monotonic() < ready_deadline
            time.sleep(0.01)
        start = time.monotonic()
        result = local_registry.kill_process(session.id)
        elapsed = time.monotonic() - start
        assert result["status"] == "killed"
        assert elapsed < 0.5, f"ordinary kill took {elapsed:.3f}s"
        assert not session.exit_path
        assert "exit_path" not in entry
    finally:
        if not session.exited:
            local_registry.kill_process(session.id)


@pytest.mark.parametrize("restart_safe", [False, True])
@pytest.mark.parametrize("late", [False, True])
def test_recorded_outliving_exit_precedes_pid_reuse(local_registry, tmp_path, monkeypatch, restart_safe, late):
    run_dir = tmp_path / "process-runs" / "proc_recorded"
    run_dir.mkdir(parents=True)
    exit_path = run_dir / "exit"
    log_path = run_dir / "out.log"
    log_path.write_text("retained tail\n", encoding="utf-8")
    session = ProcessSession(id="proc_recorded", command="exit 37", pid=987654,
                             host_start_time=1, persist_on_release=True,
                             survive_restart=restart_safe, log_path=str(log_path),
                             exit_path=str(exit_path), notify_on_complete=True,
                             systemd_unit="hermes-worker-recorded.scope", detached=late)
    local_registry._running[session.id] = session
    local_registry._write_checkpoint()
    exit_path.write_text("37\n", encoding="utf-8")
    fate = Mock(return_value="reused")
    signal = Mock()
    stop_scope = Mock()
    monkeypatch.setattr("tools.process_registry._stop_systemd_unit", stop_scope)
    if not late:
        local_registry = ProcessRegistry()
    monkeypatch.setattr(local_registry, "_detached_host_fate", fate)
    monkeypatch.setattr(local_registry, "_terminate_host_pid", signal)
    if late:
        local_registry._survivor_poller_loop(session)
    else:
        assert local_registry.recover_from_checkpoint() == 1
    recovered = local_registry.get(session.id)
    assert recovered is not None
    assert recovered.exited and recovered.exit_code == 37
    assert "retained tail" in recovered.output_buffer
    event = local_registry.completion_queue.get_nowait()
    assert event["exit_code"] == 37
    assert local_registry.completion_queue.empty()
    fate.assert_not_called()
    signal.assert_not_called()
    stop_scope.assert_not_called()
