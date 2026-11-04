"""Ordinary jobs keep pipes; outliving-job adoption reads a bounded tail snapshot."""
from pathlib import Path
import shlex
import sys
import threading
from unittest.mock import Mock

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession


def _checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.process_registry.CHECKPOINT_PATH", tmp_path / "processes.json")


@pytest.mark.platforms("posix")
def test_chatty_ordinary_job_has_no_on_disk_output(tmp_path, monkeypatch):
    _checkpoint(tmp_path, monkeypatch)
    registry = ProcessRegistry()
    ready = threading.Event()
    registry.on_output = lambda session, chunk: ready.set() if "READY" in chunk else None
    source = "import sys,time; print('x'*2000000); print('READY',flush=True); time.sleep(30)"
    session = registry.spawn_local(f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}")
    try:
        assert ready.wait(10)
        assert session.output_buffer.endswith("READY\n")
        assert len(session.output_buffer) <= session.max_output_chars
        assert not session.log_path and not session.output_log
        assert not list(tmp_path.rglob("out.log"))
        assert session.process.stdout is not None
        assert not session.exit_path, "ordinary attached jobs must not create receipts"
    finally:
        registry.kill_process(session.id)


def test_concurrent_exit_collection_cannot_reingest_log(tmp_path, monkeypatch):
    _checkpoint(tmp_path, monkeypatch)
    log, exit_path = tmp_path / "out.log", tmp_path / "exit"
    log.write_text("READY\n", encoding="utf-8")
    exit_path.write_text("0\n", encoding="utf-8")
    session = ProcessSession(id="proc_read_race", command="race", log_path=str(log),
                             exit_path=str(exit_path), watch_patterns=["READY"])
    registry = ProcessRegistry()
    registry._running[session.id] = session
    streamed = []
    registry.on_output = lambda session, text: streamed.append(text)
    monkeypatch.setattr("tools.process_registry.WATCH_MIN_INTERVAL_SECONDS", 0)
    entered, release, collected = threading.Event(), threading.Event(), threading.Event()
    original_open = Path.open
    first = True

    class HeldRead:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size=-1):
            nonlocal first
            raw = self.stream.read(size)
            if first:
                first = False
                entered.set()
                assert release.wait(10)
            return raw

    def opening(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return HeldRead(stream) if path == log and args == ("rb",) else stream

    monkeypatch.setattr(Path, "open", opening)
    poller = threading.Thread(target=registry._read_survivor_log, args=(session,))
    killer = threading.Thread(target=lambda: (registry._collect_recorded_exit(session), collected.set()))
    poller.start()
    try:
        assert entered.wait(10)
        killer.start()
        # On the broken implementation the collector finishes while the first read
        # still owns the same bytes. With serialization it waits for the owner.
        collected.wait(2)
    finally:
        release.set()
        poller.join(10)
        killer.join(10)
    assert not poller.is_alive() and not killer.is_alive()
    assert streamed == ["READY\n"]
    assert session.output_buffer == "READY\n"
    assert session._watch_hits == 1
    assert registry.completion_queue.get_nowait()["type"] == "watch_match"
    assert registry.completion_queue.empty()


def test_adoption_does_not_replay_watches_or_reset_lifetime_cap(tmp_path, monkeypatch):
    _checkpoint(tmp_path, monkeypatch)
    log = tmp_path / "out.log"
    log.write_text("READY before restart\n", encoding="utf-8")
    session = ProcessSession(id="proc_watch_adopt", command="watch", pid=12345,
                             log_path=str(log), exit_path=str(tmp_path / "exit"),
                             watch_patterns=["READY"], survive_restart=True)
    old = ProcessRegistry()
    old._running[session.id] = session
    old._write_checkpoint()
    old._read_survivor_log(session)
    assert old.completion_queue.get_nowait()["type"] == "watch_match"
    # The read must persist its updated lifetime state, not just the initial spawn.
    successor = ProcessRegistry()
    monkeypatch.setattr(successor, "_detached_host_fate", lambda *args: "running")
    monkeypatch.setattr(successor, "_survivor_poller_loop", lambda session: None)
    assert successor.recover_from_checkpoint() == 1
    adopted = successor.get(session.id)
    successor._read_survivor_log(adopted)
    assert successor.completion_queue.empty(), "historical output must not trigger watch_match again"
    assert adopted._watch_hits == 1
    assert adopted.output_buffer == "READY before restart\n"
    adopted._watch_cooldown_until = 0
    with log.open("a", encoding="utf-8") as output:
        output.write("READY after restart\n")
    successor._read_survivor_log(adopted)
    assert successor.completion_queue.get_nowait()["output"] == "READY after restart"
    assert adopted._watch_hits == 2


def test_finished_log_recovery_reads_only_a_bounded_tail(tmp_path, monkeypatch):
    _checkpoint(tmp_path, monkeypatch)
    log, exit_path = tmp_path / "out.log", tmp_path / "exit"
    with log.open("wb") as output:
        output.seek(100 * 1024 * 1024)
        output.write(b"READY tail\n")
    exit_path.write_text("7\n", encoding="utf-8")
    session = ProcessSession(id="proc_large_recover", command="large", pid=12345,
                             log_path=str(log), exit_path=str(exit_path),
                             notify_on_complete=True, watch_patterns=["READY"])
    old = ProcessRegistry()
    old._running[session.id] = session
    old._write_checkpoint()
    successor = ProcessRegistry()
    monkeypatch.setattr(successor, "_detached_host_fate", lambda *args: "gone")
    streamed = []
    successor.on_output = lambda session, text: streamed.append(text)
    original_open = Path.open
    read_bytes = 0

    class CountRead:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size=-1):
            nonlocal read_bytes
            assert 0 <= size <= session.max_output_chars
            read_bytes += size
            assert read_bytes <= session.max_output_chars, "startup replayed an unbounded finished log"
            return self.stream.read(size)

    def opening(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return CountRead(stream) if path == log and args == ("rb",) else stream

    monkeypatch.setattr(Path, "open", opening)
    assert successor.recover_from_checkpoint() == 1
    result = successor.poll(session.id)
    assert result["exit_code"] == 7
    assert "READY tail" in result["output_preview"]
    assert 0 < read_bytes <= session.max_output_chars
    assert streamed == [], "recovery is history, not a new live-output event"
    assert successor.completion_queue.get_nowait()["type"] == "completion"
    assert successor.completion_queue.empty()


def test_legacy_gone_opt_out_remains_opted_out(tmp_path, monkeypatch):
    _checkpoint(tmp_path, monkeypatch)
    registry = ProcessRegistry()
    session = ProcessSession(id="proc_legacy_silent", command="legacy", pid=12345,
                             detached=True, notify_on_complete=False)
    registry._running[session.id] = session
    registry._prune_uncollected_detached(session)
    assert session.exited and session.exit_code is None
    assert session.notify_on_complete is False
    assert registry.completion_queue.empty()


def test_shutdown_list_does_not_wait_for_fenced_survivor(tmp_path, monkeypatch):
    _checkpoint(tmp_path, monkeypatch)
    registry = ProcessRegistry()
    session = ProcessSession(id="proc_fenced_list", command="finished", survive_restart=True,
                             log_path=str(tmp_path / "out.log"), exit_path=str(tmp_path / "exit"))
    session.process = Mock()
    session.process.poll.return_value = 0
    registry._running[session.id] = session
    registry.stop_survivor_pollers()
    monkeypatch.setattr(session._completion_event, "wait", Mock(side_effect=AssertionError("list waited on a fenced poller")))
    assert registry.list_sessions()[0]["session_id"] == session.id
