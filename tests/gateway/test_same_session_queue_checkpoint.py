"""Native FIFO admission durability, including failure and restart ownership transfer."""
import json
import os
import time
from gateway import shutdown_flush
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import build_session_key
from hermes_state import SessionDB
from tests.gateway.test_steer_command import _make_runner, _make_source, _session_entry


def _event(text, mid):
    return MessageEvent(text=text, source=_make_source(), message_id=mid, message_type=MessageType.TEXT)


def test_fifo_head_and_tail_are_durable_at_ordinary_acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = _make_runner(_session_entry())
    key = build_session_key(_make_source())
    first, second = _event("inspect only", "queue1"), _event("then stop", "queue2")
    assert runner._enqueue_fifo(key, first, adapter)
    assert runner._enqueue_fifo(key, second, adapter)
    files = sorted((tmp_path / "pending_messages").glob("pending-*.json"))
    payloads = sorted((json.loads(p.read_text()) for p in files), key=lambda p: p["seq"])
    assert [p["data"]["text"] for p in payloads] == ["inspect only", "then stop"]
    assert [p["data"]["message_id"] for p in payloads] == ["queue1", "queue2"]
    assert all(p["data"]["queue_accepted"] is True for p in payloads)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in files)
    assert shutdown_flush.flush_pending_to_file(adapter._pending_messages) == 1
    assert shutdown_flush.flush_overflow_to_file({key: runner._overflow_queue(key)}) == 1
    assert len(list((tmp_path / "pending_messages").glob("pending-*.json"))) == 2


def test_failed_checkpoint_is_not_acknowledged_or_enqueued(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = _make_runner(_session_entry())
    key = build_session_key(_make_source())
    event = _event("never lose this", "queue3")
    def fail(*args, **kwargs):
        raise OSError("injected storage failure")
    monkeypatch.setattr(shutdown_flush, "_write_payload", fail)
    assert runner._enqueue_fifo(key, event, adapter) is False
    assert not getattr(event, "_gateway_accepted", False)
    assert key not in adapter._pending_messages
    assert not runner._overflow_queue(key)




def test_directory_fsync_failure_does_not_publish_initial_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    event = _event("must not replay", "queue-fsync-new")
    original_fsync = os.fsync

    def fail_directory(fd):
        if (os.fstat(fd).st_mode & 0o170000) == 0o040000:
            raise OSError("injected directory fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(shutdown_flush.os, "fsync", fail_directory)
    assert shutdown_flush.checkpoint_queued_event("key", event) is False
    assert not list((tmp_path / "pending_messages").glob("*.json"))


def test_directory_fsync_failure_preserves_existing_checkpoint_on_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    event = _event("accepted original", "queue-fsync-refresh")
    assert shutdown_flush.checkpoint_queued_event("key", event)
    path = event._hermes_recovery_spool
    original = json.loads(path.read_text())
    event.text = "rejected replacement"
    original_fsync = os.fsync

    def fail_directory(fd):
        if (os.fstat(fd).st_mode & 0o170000) == 0o040000:
            raise OSError("injected directory fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(shutdown_flush.os, "fsync", fail_directory)
    assert shutdown_flush.checkpoint_queued_event("key", event, refresh=True) is False
    assert json.loads(path.read_text()) == original


def test_queue_spool_cannot_be_consumed_as_naked_transcript_or_expired(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("same", "telegram")
    event = _event("accepted future instruction", "queue4")
    assert shutdown_flush.checkpoint_queued_event("key", event)
    path = event._hermes_recovery_spool
    old = time.time() - 3 * 24 * 60 * 60
    os.utime(path, (old, old))
    recovered = shutdown_flush.recover_pending_to_db(
        session_db=db, session_resolver=lambda *args, **kwargs: ("same", db))
    assert recovered == 0
    assert path.exists()
    assert db.get_messages("same") == []
    db.close()




def test_restart_dedup_requires_matching_platform_id_and_content(tmp_path, monkeypatch):
    """A reused platform id with different content is a distinct accepted input."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("same", "telegram")
    db.append_message("same", "user", "first version", platform_message_id="101")
    event = _event("different accepted instruction", "101")
    assert shutdown_flush.checkpoint_queued_event("key", event)
    claimed = []
    recovered, _ = shutdown_flush.recover_pending_spool(
        session_db=db, session_resolver=lambda *args, **kwargs: ("same", db),
        deferred_followup=lambda key, sid, data, path: claimed.append(data["text"]) or True,
    )
    assert recovered == 1 and claimed == ["different accepted instruction"]
    assert event._hermes_recovery_spool.exists()
    assert [row["content"] for row in db.get_messages("same") if row["role"] == "user"] == ["first version"]
    db.close()


def test_admitted_input_transfers_to_native_transcript_owner_without_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("same", "telegram")
    event = _event("accepted instruction", "queue5")
    assert shutdown_flush.checkpoint_queued_event("key", event)
    db.append_message("same", "user", event.text, platform_message_id="queue5")
    # Deliberately no assistant reply: a platform id proves admission, NOT completion.
    # The native transcript retains this unfinished turn after spool ownership transfers.
    assert shutdown_flush.recover_pending_to_db(
        session_db=db, session_resolver=lambda *args, **kwargs: ("same", db)) == 1
    assert not event._hermes_recovery_spool.exists()
    rows = db.get_messages("same")
    assert len(rows) == 1 and rows[0]["role"] == "user"
    db.close()


def test_redelivered_logical_input_is_acknowledged_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = _make_runner(_session_entry())
    key = build_session_key(_make_source())
    assert runner._enqueue_fifo(key, _event("inspect only", "dup1"), adapter)
    retry = _event("inspect only", "dup1")
    assert runner._enqueue_fifo(key, retry, adapter)
    assert retry._gateway_accepted is True
    assert not runner._overflow_queue(key)
    assert len(list((tmp_path / "pending_messages").glob("pending-*.json"))) == 1
    # A distinct input that reuses an id is not swallowed as a duplicate.
    assert runner._enqueue_fifo(key, _event("different instruction", "dup1"), adapter)
    assert [e.text for e in runner._overflow_queue(key)] == ["different instruction"]


def test_merged_head_refresh_is_one_atomic_native_spool(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    event = _event("first", "queue6")
    assert shutdown_flush.checkpoint_queued_event("key", event)
    path = event._hermes_recovery_spool
    event.text = "first\nsecond"
    event.message_id = "queue7"
    assert shutdown_flush.checkpoint_queued_event("key", event, refresh=True)
    assert event._hermes_recovery_spool == path
    assert len(list(path.parent.glob("pending-*.json"))) == 1
    assert json.loads(path.read_text())["data"]["text"] == "first\nsecond"
