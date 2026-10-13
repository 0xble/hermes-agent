"""Receipt persistence and retention must not postpone bounded process-stop signals."""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.mark.platforms("posix")
def test_reader_receipt_contention_does_not_block_bounded_shutdown(tmp_path, monkeypatch):
    """A reader already finalizing cannot hold shutdown behind an unrelated writer."""
    from tools import process_registry_results as receipts

    registry = ProcessRegistry()
    home = tmp_path / "home"
    monkeypatch.setattr(receipts, "get_hermes_home", lambda: home)
    held, release, reader_waiting, stopped = (threading.Event() for _ in range(4))
    real_acquire = receipts._acquire_result_lock

    def acquire(deadline):
        if threading.current_thread().name.startswith("proc-reader-") and deadline is None:
            reader_waiting.set()
        return real_acquire(deadline)

    monkeypatch.setattr(receipts, "_acquire_result_lock", acquire)

    def hold_writer():
        with receipts._RESULT_WRITE_LOCK:
            held.set()
            release.wait(8.0)

    holder = threading.Thread(target=hold_writer, daemon=True)
    holder.start()
    assert held.wait(2.0)
    reader_proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.readline(); print('reader terminal tail')"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    shutdown = None
    try:
        reader = registry.adopt_local(reader_proc, command="reader", cwd=None)
        reader.notify_on_complete = True
        session = ProcessSession(id="proc_reader_contention_child", command="sleep", task_id="t1",
                                 started_at=time.time())
        session.process, session.pid = child, child.pid
        registry._running[session.id] = session
        assert reader_proc.stdin is not None
        reader_proc.stdin.write("exit\n")
        reader_proc.stdin.flush()
        assert reader_waiting.wait(2.0)

        def stop():
            try:
                registry.kill_all(deadline=time.monotonic() + 0.4, source="gateway_shutdown")
            finally:
                stopped.set()

        shutdown = threading.Thread(target=stop, daemon=True)
        shutdown.start()
        assert stopped.wait(2.0), "shutdown parked behind the reader's receipt write"
        child.wait(timeout=2.0)
        assert child.returncode is not None
        # A duplicate finisher must not release a finite parent before the reader's write.
        assert registry._move_to_finished(reader) is False
        assert not reader._completion_event.is_set()
        release.set()
        assert reader._reader_thread is not None
        reader._reader_thread.join(timeout=5.0)
        assert reader._completion_event.is_set()
        record = json.loads((home / "logs" / "process-results" / f"{reader.id}.json").read_text(encoding="utf-8-sig"))
        assert record["completion_reason"] == "exited"
        assert record["output"] == "reader terminal tail\n"
        notices = []
        while not registry.completion_queue.empty():
            notices.append(registry.completion_queue.get_nowait())
        assert sum(n["session_id"] == reader.id for n in notices) == 1
    finally:
        release.set()
        holder.join(timeout=5.0)
        if shutdown is not None:
            shutdown.join(timeout=5.0)
        for proc in (reader_proc, child):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5.0)


@pytest.mark.parametrize("kill", ["all", "one"])
@pytest.mark.parametrize("pause", ["before_lock", "during_write"])
@pytest.mark.platforms("posix")
def test_final_receipt_owner_cannot_be_overwritten_by_kill_race(tmp_path, monkeypatch, kill, pause):
    """A real child's reader and kill path share one final receipt owner."""
    from tools import process_registry_results as receipts

    registry = ProcessRegistry()
    home = tmp_path / "home"
    monkeypatch.setattr(receipts, "get_hermes_home", lambda: home)
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.readline(); print('final tail')"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    killer = None
    release_reader = threading.Event()
    reader_waiting = threading.Event()
    session = None
    try:
        session = registry.adopt_local(child, command="reader-kill-race", cwd=None)
        session.notify_on_complete = True
        assert session._reader_thread is not None
        reader_ident = session._reader_thread.ident
        assert reader_ident is not None
        real_acquire = receipts._acquire_result_lock
        real_signal = os.kill
        real_write = receipts.atomic_json_write
        completed_writes = []

        def acquire(deadline):
            if pause == "before_lock" and threading.current_thread().ident == reader_ident:
                reader_waiting.set()
                assert release_reader.wait(10.0)
            return real_acquire(deadline)

        def signal_child(pid, sig):
            if pid == child.pid and sig == signal.SIGTERM:
                # The kill already selected a live target. Let its real reader observe exit 0
                # and claim finalization, then park it before the result-write lock.
                assert child.stdin is not None
                child.stdin.write("exit\n")
                child.stdin.flush()
                assert reader_waiting.wait(5.0)
                assert session.exit_code == 0
            return real_signal(pid, sig)

        def record_write(path, record, **kwargs):
            if record.get("completion_reason") != "cut_pending":
                completed_writes.append((threading.current_thread().ident, record.copy()))
            if pause == "during_write" and threading.current_thread().ident == reader_ident:
                reader_waiting.set()
                assert release_reader.wait(10.0)
            return real_write(path, record, **kwargs)

        monkeypatch.setattr(receipts, "_acquire_result_lock", acquire)
        monkeypatch.setattr(os, "kill", signal_child)
        monkeypatch.setattr(receipts, "atomic_json_write", record_write)
        if kill == "all":
            killer = threading.Thread(
                target=lambda: registry.kill_all(
                    deadline=time.monotonic() + 2.0, source="gateway_shutdown",
                ),
                daemon=True,
            )
        else:
            killer = threading.Thread(
                target=lambda: registry.kill_process(session.id, consume_output=False), daemon=True,
            )
        killer.start()
        assert reader_waiting.wait(5.0), "reader did not claim finalization before the kill"
        killer.join(timeout=5.0)
        assert not killer.is_alive(), "kill path waited for the reader's receipt write"
        assert session.completion_reason == "killed"
        assert not session._completion_event.is_set()
        release_reader.set()
        session._reader_thread.join(timeout=5.0)
        assert session._completion_event.is_set()
        receipt = home / "logs" / "process-results" / f"{session.id}.json"
        record = json.loads(receipt.read_text(encoding="utf-8-sig"))
        assert record["completion_reason"] == "killed"
        assert record["exit_code"] == -15
        assert record["termination_source"] == ("gateway_shutdown" if kill == "all" else "process.kill")
        assert record["output"] == "final tail\n"
        assert completed_writes
        assert all(writer == reader_ident for writer, _ in completed_writes)
        if pause == "before_lock":
            assert len(completed_writes) == 1
        notice = registry.completion_queue.get_nowait()
        assert (notice["completion_reason"], notice["exit_code"]) == ("killed", -15)
        assert registry.completion_queue.empty()
    finally:
        release_reader.set()
        if killer is not None:
            killer.join(timeout=5.0)
        if session is not None and session._reader_thread is not None:
            session._reader_thread.join(timeout=5.0)
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5.0)


@pytest.mark.parametrize("kill", ["all", "one"])
@pytest.mark.platforms("posix")
def test_kill_refreshes_receipt_after_reader_finalization(tmp_path, monkeypatch, kill):
    """SIGTERM's reader receipt must be refreshed if the kill commits afterward."""
    from tools import process_registry_results as receipts

    registry = ProcessRegistry()
    home = tmp_path / "home"
    monkeypatch.setattr(receipts, "get_hermes_home", lambda: home)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    session = None
    try:
        session = registry.adopt_local(child, command="reader-first-kill", cwd=None)
        reader = session._reader_thread
        assert reader is not None
        receipt = home / "logs" / "process-results" / f"{session.id}.json"
        real_signal = os.kill
        reader_receipts = []

        def signal_child(pid, sig):
            result = real_signal(pid, sig)
            if pid == child.pid and sig == signal.SIGTERM:
                reader.join(timeout=5.0)
                assert not reader.is_alive(), "reader did not finalize during signal grace"
                record = json.loads(receipt.read_text(encoding="utf-8-sig"))
                assert record["completion_reason"] == "exited"
                reader_receipts.append(record)
            return result

        monkeypatch.setattr(os, "kill", signal_child)
        if kill == "all":
            assert registry.kill_all(source="gateway_shutdown") == 1
        else:
            result = registry.kill_process(session.id, consume_output=False)
            assert result["status"] == "killed"
        reader.join(timeout=5.0)
        assert len(reader_receipts) == 1
        assert session.completion_reason == "killed"
        record = json.loads(receipt.read_text(encoding="utf-8-sig"))
        assert record["completion_reason"] == "killed"
        assert record["exit_code"] == -15
        assert record["termination_source"] == ("gateway_shutdown" if kill == "all" else "process.kill")
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5.0)
        if session is not None and session._reader_thread is not None:
            session._reader_thread.join(timeout=5.0)


def test_output_drain_does_not_retry_a_stable_terminal_receipt(tmp_path, monkeypatch):
    """A bounded amount of new output must not cause repeated terminal writes."""
    from tools import process_registry_results as receipts

    monkeypatch.setattr(receipts, "get_hermes_home", lambda: tmp_path)
    session = ProcessSession(id="output-drain", command="draining-child", cwd=None)
    session.mark_exited(-15, "killed", "process.kill")
    session.append_output("initial output\n")
    writes = []
    real_write = receipts.atomic_json_write

    def write_then_drain(path, record, **kwargs):
        real_write(path, record, **kwargs)
        writes.append(record)
        # Stop after three changes so the old full-snapshot comparison fails finitely.
        if len(writes) <= 3:
            session.append_output("late output\n")

    monkeypatch.setattr(receipts, "atomic_json_write", write_then_drain)
    receipts.save_completed_result(session, prune=False)
    assert len(writes) == 1
    record = json.loads((tmp_path / "logs" / "process-results" / f"{session.id}.json").read_text(encoding="utf-8-sig"))
    assert record["completion_reason"] == "killed"
    assert record["exit_code"] == -15


@pytest.mark.parametrize("phase", ["classify", "age_delete", "cap_delete"])
def test_prune_stops_classifying_after_deadline(tmp_path, monkeypatch, phase):
    """Expiry stops both legacy classification and deletion; later pruning repairs the cap."""
    from tools import process_registry_results as receipts

    home = tmp_path / "home"
    directory = home / "logs" / "process-results"
    directory.mkdir(parents=True)
    monkeypatch.setattr(receipts, "get_hermes_home", lambda: home)
    for index in range(320):
        path = directory / f"proc_legacy_{index}.json"
        path.write_text(json.dumps({
            "output": "x" * 100_000, "completion_reason": "exited",
        }))
        if phase == "age_delete":
            os.utime(path, (0, 0))
    clock = [0.0]
    classified, deleted = [], []
    real_classify = receipts._is_cut_receipt
    real_unlink = type(directory).unlink

    def classify(path):
        classified.append(path)
        result = real_classify(path)
        if phase == "classify":
            clock[0] = 2.0  # The first full legacy read consumes the remaining budget.
        return result

    def unlink(path, **kwargs):
        real_unlink(path, **kwargs)
        if path.parent == directory:
            deleted.append(path)
            clock[0] = 2.0

    monkeypatch.setattr(receipts.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(receipts, "_is_cut_receipt", classify)
    monkeypatch.setattr(type(directory), "unlink", unlink)
    receipts.prune_completed_results(deadline=1.0)

    assert len(classified) == (320 if phase == "cap_delete" else 1)
    assert len(deleted) == (0 if phase == "classify" else 1)
    assert len(list(directory.glob("proc_*.json"))) == 320 - len(deleted)
    # A later save/read with no deadline still applies the normal retention policy.
    receipts.prune_completed_results()
    expected = 0 if phase == "age_delete" else receipts.MAX_RETAINED_RESULTS
    assert len(list(directory.glob("proc_*.json"))) == expected
