"""Shutdown's adoption fence must not cut ordinary file-backed completions."""

from pathlib import Path
import shlex
import threading
import time
import shutil

import pytest

from tools.process_registry import ProcessRegistry


def _spawn_gated(registry, tmp_path, **kwargs):
    gate = tmp_path / "release-job"
    session = registry.spawn_local(
        f"while [ ! -e {shlex.quote(str(gate))} ]; do sleep 0.02; done; printf 'done-ok\\n'; exit 0",
        cwd=str(tmp_path), task_id="task", session_key="session", **kwargs,
    )
    session.notify_on_complete = True
    registry._write_checkpoint()
    return session, gate


def _assert_normal_completion(registry, session, caplog):
    result = registry.wait(session.id, timeout=5)
    assert result["exit_code"] == 0, result
    assert result["completion_reason"] == "exited", result
    assert result["termination_source"] == "", result
    assert result["output"] == "done-ok\n", result
    event = registry.completion_queue.get(timeout=5)
    assert event["exit_code"] == 0, event
    assert event["completion_reason"] == "exited", event
    assert event["termination_source"] == "", event
    assert registry.completion_queue.empty()
    assert "start-time mismatch" not in caplog.text


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["ordinary", "persist_on_release", "survive_restart"])
def test_shutdown_fence_only_leaves_restart_safe_exit_for_adoption(tmp_path, monkeypatch, caplog, kind):
    # Bash -lic emits logout on explicit exit on both Linux and macOS.
    # Use zsh to keep the exact output assertion on command output, not shell noise.
    shell = shutil.which("zsh")
    if shell is None:
        pytest.skip("zsh is required for exact command-output assertions")
    monkeypatch.setattr("tools.process_registry._find_shell", lambda: shell)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    registry = ProcessRegistry()
    session, gate = _spawn_gated(registry, tmp_path, **({kind: True} if kind != "ordinary" else {}))
    try:
        registry.stop_survivor_pollers()
        gate.touch()
        assert session.process.wait(timeout=5) == 0
        if kind == "ordinary":
            assert not session.exit_path
        else:
            assert Path(session.exit_path).read_text(encoding="utf-8-sig").strip() == "0"
        session._reader_thread.join(5)
        assert not session._reader_thread.is_alive()
        if kind == "survive_restart":
            assert not session.exited
            assert not session._completion_event.is_set()
            assert registry.completion_queue.empty()
            assert registry.kill_all(source="gateway_shutdown", deadline=time.monotonic() + 5) == 0
            successor = ProcessRegistry()
            assert successor.recover_from_checkpoint() == 1
            _assert_normal_completion(successor, successor.get(session.id), caplog)
        else:
            assert session.exited, "ordinary completions must finalize during the shutdown drain"
            assert registry.kill_all(source="gateway_shutdown", deadline=time.monotonic() + 5) == 0
            _assert_normal_completion(registry, session, caplog)
    finally:
        gate.touch()
        session.process.wait(timeout=5)
        registry._survivor_pollers_stopped.clear()
        if session.exit_path:
            registry._survivor_poller_loop(session)
        session._reader_thread.join(5)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("sweep", ["bounded", "unbounded", "explicit"])
def test_shutdown_collects_published_exit_before_signalling_delayed_reader(
    tmp_path, monkeypatch, caplog, sweep,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    registry = ProcessRegistry()
    release_reader = threading.Event()
    reader_name = "_survivor_poller_loop"
    real_poller = getattr(registry, reader_name)

    def delayed_poller(session):
        assert release_reader.wait(15)
        real_poller(session)

    monkeypatch.setattr(registry, reader_name, delayed_poller)
    session, gate = _spawn_gated(registry, tmp_path, persist_on_release=True)
    try:
        registry.stop_survivor_pollers()
        gate.touch()
        assert session.process.wait(timeout=5) == 0
        assert Path(session.exit_path).read_text(encoding="utf-8-sig").strip() == "0"
        assert not session.exited
        if sweep == "explicit":
            result = registry.kill_process(session.id, consume_output=False)
            assert result["status"] == "already_exited", result
        else:
            kwargs = {"deadline": time.monotonic() + 5} if sweep == "bounded" else {}
            assert registry.kill_all(source="gateway_shutdown", **kwargs) == 0
        release_reader.set()  # Pipe finalization stays with its original reader.
        _assert_normal_completion(registry, session, caplog)
    finally:
        gate.touch()
        session.process.wait(timeout=5)
        registry._survivor_pollers_stopped.clear()
        release_reader.set()
        session._reader_thread.join(5)


@pytest.mark.platforms("posix")
def test_persistent_completion_during_shutdown_drain_reaches_successor(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    registry = ProcessRegistry()
    session, gate = _spawn_gated(registry, tmp_path, persist_on_release=True)
    assert session.process is not None and session._reader_thread is not None
    try:
        # Gateway teardown fences before draining agents. Its queue need not have
        # another consumer, even though this ordinary poller still collects exit.
        registry.stop_survivor_pollers()
        gate.touch()
        assert session.process.wait(timeout=5) == 0
        session._reader_thread.join(5)
        assert not session._reader_thread.is_alive()
        assert session.exited
        assert session._completion_event.is_set()
        assert registry.kill_all(source="gateway_shutdown", deadline=time.monotonic() + 5) == 0

        successor = ProcessRegistry()
        assert successor.recover_from_checkpoint() == 1, "drain completion lost with the old gateway queue"
        notices = successor.drain_notifications(session_key="session")
        assert len(notices) == 1
        event, text = notices[0]
        assert event["session_id"] == session.id
        assert event["owner_task_id"] == "task"
        assert event["exit_code"] == 0
        assert event["completion_reason"] == "exited"
        assert event["termination_source"] == ""
        assert "done-ok" in event["output"] and "done-ok" in text
        adopted = successor.get(session.id)
        assert adopted is not None
        assert adopted.persist_on_release
        assert not adopted.survive_restart and not adopted.delivered_path
        assert successor.drain_notifications(session_key="session") == []
        # Ordinary delivery stays ordinary: not restart-safe repeated replay.
        assert ProcessRegistry().recover_from_checkpoint() == 0
    finally:
        gate.touch()
        session.process.wait(timeout=5)
        session._reader_thread.join(5)
