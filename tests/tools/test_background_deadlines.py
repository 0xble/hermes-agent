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


def test_unreachable_recovered_pid_is_retired_as_lost(registry, monkeypatch):
    session = ProcessSession(id="unreachable", command="verification", task_id="test", started_at=time.time())
    session.notify_on_complete = True
    registry._running[session.id] = session
    monkeypatch.setattr(registry, "_signal_kill", lambda *a, **k: {"status": "already_exited"})
    registry.set_deadline(session.id, 0.01)
    assert session._completion_event.wait(3)
    result = registry.poll(session.id)
    assert result["completion_reason"] == "lost"
    assert result["termination_source"] == "deadline_unconfirmed"
    assert result["exit_code"] is None
    notice = registry.completion_queue.get(timeout=2)
    assert notice["session_id"] == session.id


def _unconfirmed_session(registry, monkeypatch, *, retry=0.05):
    """A running session whose deadline kill keeps erroring; returns (session, attempts)."""
    monkeypatch.setattr(module, "DEADLINE_KILL_RETRY_SECONDS", retry)
    monkeypatch.setattr(module, "DEADLINE_KILL_RETRY_MAX_SECONDS", retry * 4)
    session = ProcessSession(id="proc_unconfirmed", command="verification", task_id="test",
                             owner_task_id="sa-owner", session_key="chat", started_at=time.time())
    session.notify_on_complete = True
    registry._running[session.id] = session
    attempts = []

    def failing_kill(*args, **kwargs):
        attempts.append(time.monotonic())
        raise OSError("backend unavailable")

    monkeypatch.setattr(registry, "_signal_kill", failing_kill)
    return session, attempts


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_unconfirmed_kill_keeps_process_running_and_retries(registry, monkeypatch):
    session, attempts = _unconfirmed_session(registry, monkeypatch)
    registry.set_deadline(session.id, 0.01)
    assert _wait_for(lambda: len(attempts) >= 2), "deadline worker did not retry the failed kill"
    # Not retired: still running, still recoverable, still owned, no completion fired.
    assert not session.exited
    assert not session._completion_event.is_set()
    assert registry.get(session.id) is session and session.id in registry._running
    assert registry.running_owned_by("sa-owner") == [session]
    assert registry.has_active_for_session("chat") and registry.has_any_active()
    assert registry.completion_queue.empty()
    listed = {s["session_id"]: s for s in registry.list_sessions("test")}[session.id]
    assert listed["status"] == "running" and listed["termination_unconfirmed"] is True
    assert listed["termination_attempts"] >= 2
    polled = registry.poll(session.id)
    assert polled["status"] == "running" and polled["termination_unconfirmed"] is True
    assert polled["termination_error"] == "backend unavailable"
    assert "could not be confirmed" in polled["output_preview"]
    assert polled["output_preview"].count("could not be confirmed") == 1
    waited = registry.wait(session.id, timeout=1)
    assert waited["status"] == "timeout" and waited["termination_unconfirmed"] is True
    # The live handle is still there for explicit cleanup: a kill is attempted, not short-circuited.
    before = len(attempts)
    explicit = registry.kill_process(session.id)
    assert explicit["status"] == "error" and len(attempts) == before + 1
    # Retries are paced, not a tight loop.
    assert _wait_for(lambda: len(attempts) >= 3)
    assert attempts[2] - attempts[1] >= 0.09
    assert attempts[1] - attempts[0] >= 0.04
    checkpoint = {e["session_id"]: e for e in json.loads(module.CHECKPOINT_PATH.read_text())}
    assert checkpoint[session.id]["termination_attempts"] >= 2
    assert checkpoint[session.id]["termination_unconfirmed_at"] > 0
    session._completion_event.set()  # stop the worker before fixture teardown


def test_unconfirmed_kill_retires_on_later_confirmed_kill(registry, monkeypatch):
    session, attempts = _unconfirmed_session(registry, monkeypatch)
    registry.set_deadline(session.id, 0.01)
    assert _wait_for(lambda: len(attempts) >= 1)
    monkeypatch.setattr(registry, "_signal_kill", lambda *a, **k: None)
    assert session._completion_event.wait(5), "confirmed kill did not finish the session"
    result = registry.poll(session.id)
    assert result["status"] == "exited"
    assert result["exit_code"] == 124 and result["completion_reason"] == "timed_out"
    assert result["termination_source"] == "terminal.timeout"
    assert "termination_unconfirmed" not in result
    assert session.id not in registry._running
    notice = registry.completion_queue.get(timeout=2)
    assert notice["session_id"] == session.id and notice["completion_reason"] == "timed_out"
    assert "could not be confirmed" in notice["output"]


def test_unconfirmed_kill_retires_on_observed_exit(registry, monkeypatch):
    session, attempts = _unconfirmed_session(registry, monkeypatch, retry=0.5)
    registry.set_deadline(session.id, 0.01)
    assert _wait_for(lambda: len(attempts) >= 1)
    # The observer (reader/poller) sees the exit while the worker is backing off.
    registry._finish_exited(session, 0)
    assert session._completion_event.wait(3)
    assert session.exited and session.id in registry._finished
    assert registry.completion_queue.get(timeout=2)["session_id"] == session.id
    session._deadline_thread.join(3)
    assert not session._deadline_thread.is_alive()
    assert len(attempts) == 1


def test_unconfirmed_backoff_is_bounded(registry, monkeypatch):
    monkeypatch.setattr(registry, "_write_checkpoint", lambda *a, **k: None)
    session = ProcessSession(id="proc_backoff", command="x", started_at=time.time())
    delays = [registry._record_unconfirmed_termination(session, f"err {i}") for i in range(6)]
    assert delays == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]
    assert session.termination_attempts == 6 and session.termination_error == "err 5"
    assert not session.exited


def test_unconfirmed_state_survives_checkpoint_recovery(registry, monkeypatch):
    session = ProcessSession(id="proc_recover_unconfirmed", command="test", pid=1234, host_start_time=5678,
                             started_at=time.time() - 20, deadline_at=time.time() - 5,
                             termination_attempts=3, termination_unconfirmed_at=time.time() - 1)
    registry._running[session.id] = session
    registry._write_checkpoint()
    recovered = ProcessRegistry()
    monkeypatch.setattr(recovered, "_host_pid_is_ours", lambda *args: True)
    monkeypatch.setattr(recovered, "_start_deadline", Mock())
    assert recovered.recover_from_checkpoint() == 1
    restored = recovered.get(session.id)
    assert restored.termination_attempts == 3 and restored.termination_unconfirmed_at > 0
    assert not restored.exited
    registry._running.clear()
    recovered._running.clear()


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
