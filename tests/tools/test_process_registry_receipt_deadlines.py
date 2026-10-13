"""Receipt persistence and retention must not postpone bounded process-stop signals."""

import json
import os
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
        record = json.loads((home / "logs" / "process-results" / f"{reader.id}.json").read_text())
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
