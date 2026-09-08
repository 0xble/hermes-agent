"""Explicit background runtime deadlines, not poll-window timeouts."""
import json
import shlex
import shutil
import sys
import time
from unittest.mock import Mock

import pytest

from tools import process_registry as module
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    monkeypatch.setattr(module, "_SYSTEMD_SCOPE_AVAILABLE", False)
    value = ProcessRegistry()
    yield value
    value.kill_all()


def test_explicit_deadline_exits_once_and_preserves_timeout_result(registry):
    session = registry.spawn_local(f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'")
    session.notify_on_complete = True
    registry.set_deadline(session.id, 1)
    assert session._completion_event.wait(8), "deadline did not finish the process"
    result = registry.poll(session.id)
    assert result["exit_code"] == 124
    assert result["completion_reason"] == "timed_out"
    assert result["termination_source"] == "terminal.timeout"
    assert session.process is not None and session.process.poll() is not None
    assert not registry.is_completion_consumed(session.id)
    notification = registry.completion_queue.get(timeout=1)
    assert notification["completion_reason"] == "timed_out"
    session.mark_exited(-15)
    registry._move_to_finished(session)
    assert session.exit_code == 124
    assert registry.completion_queue.empty()


def test_large_finite_deadline_keeps_watchdog_alive(registry):
    session = registry.spawn_local(f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'", "test")
    registry.set_deadline(session.id, 1e12)
    session._deadline_thread.join(0.1)
    assert session._deadline_thread.is_alive()
    assert session.deadline_at > 1e11
    assert not session.exited


def test_omitted_deadline_is_unbounded(registry):
    session = registry.spawn_local(f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'")
    assert session.deadline_at == 0
    assert session._deadline_thread is None
    assert not session.exited


def test_completed_process_is_not_reclassified_as_timed_out(registry):
    session = registry.spawn_local(f"{shlex.quote(sys.executable)} -c 'pass'")
    assert session._completion_event.wait(8)
    registry.set_deadline(session.id, 1)
    assert session.exit_code == 0
    assert session.completion_reason == "exited"


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan"), True])
def test_invalid_deadline_rejected(registry, seconds):
    with pytest.raises(ValueError):
        registry.set_deadline("irrelevant", seconds)


def test_checkpoint_recovers_original_deadline_not_fresh_budget(registry, monkeypatch):
    session = ProcessSession(id="proc_recover", command="test", pid=1234, host_start_time=5678,
                             started_at=time.time() - 20, deadline_at=time.time() + 30)
    registry._running[session.id] = session
    registry._write_checkpoint()
    saved = json.loads(module.CHECKPOINT_PATH.read_text())[0]
    assert saved["deadline_at"] == session.deadline_at
    recovered = ProcessRegistry()
    monkeypatch.setattr(recovered, "_host_pid_is_ours", lambda *args: True)
    start = Mock()
    monkeypatch.setattr(recovered, "_start_deadline", start)
    assert recovered.recover_from_checkpoint() == 1
    restored = recovered.get(session.id)
    assert restored is not None and restored.deadline_at == session.deadline_at
    start.assert_called_once()
    # Synthetic PID only. Never signal it during fixture cleanup.
    registry._running.clear()
    recovered._running.clear()


@pytest.mark.skipif(sys.platform == "win32", reason="bash job-control boundary")
@pytest.mark.parametrize("outer_shell", ["bash", "sh", "zsh"])
def test_sandbox_deadline_terminates_command_not_only_wrapper(registry, tmp_path, outer_shell):
    """Exercise the sandbox shell wrapper and group kill with real bash processes."""
    import subprocess
    import tempfile

    if not shutil.which(outer_shell):
        pytest.skip(f"{outer_shell} not installed")

    class ShellEnv:
        def get_temp_dir(self):
            return str(tmp_path)

        def execute(self, command, timeout=10, **kwargs):
            with tempfile.TemporaryFile() as output:
                proc = subprocess.Popen([outer_shell, "-c", command], stdout=output, stderr=output,
                                        start_new_session=True)
                proc.wait(timeout=timeout)
                output.seek(0)
                return {"output": output.read().decode(), "returncode": proc.returncode}

    child_pid_path = tmp_path / "child.pid"
    graceful = tmp_path / "graceful"
    script = (
        "import os,time,signal,sys\n"
        "def stop(*args):\n"
        " time.sleep(0.2)\n"
        f" open({str(graceful)!r},'w').write('flushed')\n"
        " sys.exit(0)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        f"open({str(child_pid_path)!r},'w').write(str(os.getpid()))\n"
        "time.sleep(30)"
    )
    session = registry.spawn_via_env(ShellEnv(), f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}")
    registry.set_deadline(session.id, 2)
    assert session._completion_event.wait(10)
    assert session.completion_reason == "timed_out"
    assert child_pid_path.exists(), "child never reached the verification body"
    assert graceful.read_text() == "flushed", "SIGKILL preempted graceful shutdown"
    child = int(child_pid_path.read_text())
    # A zombie has stopped executing too, but kill(pid, 0) still succeeds on Linux.
    observed = subprocess.run(["ps", "-o", "stat=", "-p", str(child)], capture_output=True, text=True)
    assert not observed.stdout.strip() or observed.stdout.strip().startswith("Z")


@pytest.mark.skipif(sys.platform == "win32", reason="bash sandbox signal boundary")
def test_successful_kill_signal_requires_confirmed_exit(registry):
    import subprocess

    class UnstoppableEnv:
        def execute(self, command, timeout=5):
            script = shlex.split(command)[-1]
            # Simulate successful signal delivery but a still-live process group.
            prefix = "kill() { return 0; }; sleep() { :; }; "
            result = subprocess.run(["bash", "-c", prefix + script], timeout=timeout, capture_output=True, text=True)
            return {"returncode": result.returncode, "output": result.stdout}

    session = ProcessSession(id="unstoppable", command="verify", task_id="test", started_at=time.time())
    session.env_ref = UnstoppableEnv()
    session.pid = 12345
    session.sandbox_process_group = True
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        registry._signal_kill(session, session.id, False)


@pytest.mark.parametrize("already_gone", [False, True])
def test_unconfirmed_termination_wakes_owner_as_lost(registry, monkeypatch, already_gone):
    session = ProcessSession(id="unreachable", command="verification", task_id="test", started_at=time.time())
    session.notify_on_complete = True
    registry._running[session.id] = session

    def unavailable(*args, **kwargs):
        if already_gone:
            return {"status": "already_exited"}
        raise OSError("backend unavailable")

    monkeypatch.setattr(registry, "_signal_kill", unavailable)
    registry.set_deadline(session.id, 0.01)
    assert session._completion_event.wait(3)
    result = registry.poll(session.id)
    assert result["completion_reason"] == "lost"
    assert result["termination_source"] == "deadline_unconfirmed"
    assert result["exit_code"] is None
    notice = registry.completion_queue.get(timeout=2)
    assert notice["session_id"] == session.id
    if not already_gone:
        assert "Reconcile external effects" in notice["output"]


def test_real_recovered_deadline_rearms_without_extending(registry, monkeypatch):
    # Simulate the old owner disappearing after checkpoint, without a timer
    # from that owner racing recovery. The child is a real isolated process.
    monkeypatch.setattr(registry, "_start_deadline", Mock())
    session = registry.spawn_local(f"{shlex.quote(sys.executable)} -c 'import time;time.sleep(30)'")
    registry.set_deadline(session.id, 2)
    recovered = ProcessRegistry()
    try:
        assert recovered.recover_from_checkpoint() == 1
        restored = recovered.get(session.id)
        assert restored is not None and restored.deadline_at == session.deadline_at
        assert restored._completion_event.wait(10)
        assert restored.exit_code == 124 and restored.completion_reason == "timed_out"
        assert session.process is not None
        session.process.wait(timeout=5)
    finally:
        recovered.kill_all()
