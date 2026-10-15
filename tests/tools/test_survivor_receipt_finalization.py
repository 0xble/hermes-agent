"""Restart survival must coexist with the cut-receipt finalization owner."""

import json
import shlex
import threading
import time

import pytest

from tools.process_registry import ProcessRegistry


@pytest.mark.platforms("posix")
def test_shutdown_fence_does_not_wait_for_survivor_receipt_writer(tmp_path, monkeypatch):
    """Receipt IO cannot block shutdown or publish an offer into the stopped gateway."""
    from tools import process_registry as module
    from tools import process_registry_results as receipts

    home = tmp_path / "home"
    monkeypatch.setattr(module, "get_hermes_home", lambda: home)
    monkeypatch.setattr(receipts, "get_hermes_home", lambda: home)
    held, release, finalizing, stopped = (threading.Event() for _ in range(4))
    real_acquire = receipts._acquire_result_lock

    def acquire(deadline):
        if threading.current_thread().name.startswith("proc-survivor-poller-"):
            finalizing.set()
        return real_acquire(deadline)

    monkeypatch.setattr(receipts, "_acquire_result_lock", acquire)

    def hold_writer():
        with receipts._RESULT_WRITE_LOCK:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold_writer, daemon=True)
    holder.start()
    assert held.wait(2)
    registry = ProcessRegistry()
    gate = tmp_path / "release-job"
    session = registry.spawn_local(
        f"while [ ! -e {shlex.quote(str(gate))} ]; do sleep 0.02; done; printf 'durable tail\\n'; exit 7",
        cwd=str(tmp_path), task_id="task", session_key="session", survive_restart=True,
    )
    session.notify_on_complete = True
    registry._write_checkpoint()
    shutdown = None
    try:
        gate.touch()
        assert finalizing.wait(5)
        assert session._finish_claimed and not session._result_owner_write_done

        def stop():
            registry.stop_survivor_pollers()
            registry.kill_all(source="gateway_shutdown", deadline=time.monotonic() + 0.4)
            stopped.set()

        shutdown = threading.Thread(target=stop, daemon=True)
        shutdown.start()
        assert stopped.wait(2), "shutdown fence waited for unrelated receipt IO"
        assert registry._move_to_finished(session) is False
        assert not session._completion_event.is_set()
        release.set()
        assert session._completion_event.wait(5)
        assert session._result_owner_write_done
        assert registry.completion_queue.empty(), "stopped producer must leave delivery to its successor"
        receipt = home / "logs" / "process-results" / f"{session.id}.json"
        record = json.loads(receipt.read_text(encoding="utf-8-sig"))
        assert (record["completion_reason"], record["exit_code"], record["cut_receipt"]) == ("exited", 7, False)
        assert record["output"] == "durable tail\n"
        successor = ProcessRegistry()
        assert successor.recover_from_checkpoint() == 1
        assert successor.get(session.id)._completion_event.wait(5)
        offers = successor.drain_notifications(session_key="session")
        assert len(offers) == 1 and offers[0][0]["exit_code"] == 7
        successor.acknowledge_survivor_completion(session.id)
        assert successor.drain_notifications(session_key="session") == []
        assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig")) == []
    finally:
        release.set()
        holder.join(5)
        if shutdown is not None:
            shutdown.join(5)
        if session.process.poll() is None:
            registry.kill_process(session.id)
        session.process.wait(timeout=5)
        session._reader_thread.join(5)


@pytest.mark.platforms("posix")
def test_bounded_shutdown_cuts_only_ordinary_jobs_and_preserves_survivor_result(tmp_path, monkeypatch):
    from tools import process_registry as module
    from tools import process_registry_results as receipts

    home = tmp_path / "home"
    monkeypatch.setattr(module, "get_hermes_home", lambda: home)
    monkeypatch.setattr(receipts, "get_hermes_home", lambda: home)
    registry = ProcessRegistry()
    gate = tmp_path / "release-job"
    survivor = registry.spawn_local(
        f"while [ ! -e {shlex.quote(str(gate))} ]; do sleep 0.02; done; printf 'survived\\n'; exit 9",
        cwd=str(tmp_path), task_id="task", session_key="session", survive_restart=True,
    )
    survivor.notify_on_complete = True
    ordinary = registry.spawn_local("sleep 30", cwd=str(tmp_path), task_id="task")
    try:
        assert registry.kill_all(source="gateway_shutdown", deadline=time.monotonic() + 3) == 1
        assert survivor.process.poll() is None
        assert not survivor._finish_claimed and not survivor._cut_receipt
        assert survivor._kill_deadline is None and not survivor._kill_requested
        receipt_root = home / "logs" / "process-results"
        assert not (receipt_root / f"{survivor.id}.json").exists()
        cut = json.loads((receipt_root / f"{ordinary.id}.json").read_text(encoding="utf-8-sig"))
        assert cut["cut_receipt"] and cut["termination_source"] == "gateway_shutdown"
        gate.touch()
        assert survivor._completion_event.wait(5)
        result = json.loads((receipt_root / f"{survivor.id}.json").read_text(encoding="utf-8-sig"))
        assert (result["completion_reason"], result["exit_code"], result["cut_receipt"]) == ("exited", 9, False)
        offers = registry.drain_notifications(session_key="session")
        assert len(offers) == 1 and offers[0][0]["output"] == "survived\n"
        registry.acknowledge_survivor_completion(survivor.id)
        registry._move_to_finished(survivor)
        assert registry.drain_notifications(session_key="session") == []
    finally:
        registry.kill_all(source="test_cleanup")
        for session in (survivor, ordinary):
            session.process.wait(timeout=5)
            session._reader_thread.join(5)
