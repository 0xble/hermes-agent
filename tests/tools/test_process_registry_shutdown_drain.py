"""Shutdown's adoption fence must not cut ordinary file-backed completions."""

from pathlib import Path
import shlex
import threading
import time

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
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    registry = ProcessRegistry()
    session, gate = _spawn_gated(registry, tmp_path, **({kind: True} if kind != "ordinary" else {}))
    try:
        registry.stop_survivor_pollers()
        gate.touch()
        assert session.process.wait(timeout=5) == 0
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
        registry._survivor_poller_loop(session)
        session._reader_thread.join(5)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("persist", [False, True], ids=["ordinary", "persist_on_release"])
@pytest.mark.parametrize("sweep", ["bounded", "unbounded", "explicit"])
def test_shutdown_collects_published_exit_before_signalling_delayed_reader(
    tmp_path, monkeypatch, caplog, persist, sweep,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    registry = ProcessRegistry()
    release_reader = threading.Event()
    real_poller = registry._survivor_poller_loop

    def delayed_poller(session):
        assert release_reader.wait(15)
        real_poller(session)

    monkeypatch.setattr(registry, "_survivor_poller_loop", delayed_poller)
    session, gate = _spawn_gated(registry, tmp_path, persist_on_release=persist)
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
        _assert_normal_completion(registry, session, caplog)
    finally:
        gate.touch()
        session.process.wait(timeout=5)
        registry._survivor_pollers_stopped.clear()
        release_reader.set()
        session._reader_thread.join(5)
