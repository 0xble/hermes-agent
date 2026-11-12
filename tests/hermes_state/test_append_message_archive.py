"""Rejected user input is durable display history, never active model replay."""

import os

import pytest

from hermes_state import SessionDB
from hermes_state_errors import CompressionSessionClosedError, SessionTurnLeaseLostError


@pytest.fixture
def db(tmp_path):
    handle = SessionDB(tmp_path / "state.db")
    handle.create_session("s", "cli")
    yield handle
    handle.close()


def test_archived_append_preserves_identity_and_live_history(db):
    db.append_message("s", "user", "previous ask")
    db.append_message("s", "assistant", "previous answer")
    replay_before = db.get_messages_as_conversation("s")
    resume_before = db.get_resume_conversations("s")[0]
    uid = "a" * 32
    row_id = db.append_message(
        "s", "user", "rejected needle", archived=True, message_uid=uid,
        timestamp=1700000000.5, platform_message_id="platform-123",
        display_kind="text", display_metadata={"source": "test"},
    )
    assert db.get_messages_as_conversation("s") == replay_before
    rows = db.get_messages("s", include_compacted=True)
    assert [row["content"] for row in rows] == ["previous ask", "previous answer", "rejected needle"]
    row = rows[-1]
    assert row["id"] == row_id
    assert row["message_uid"] == uid
    assert row["timestamp"] == 1700000000.5
    assert row["platform_message_id"] == "platform-123"
    assert row["display_kind"] == "text"
    assert row["display_metadata"] == {"source": "test"}
    flags = db._conn.execute("SELECT active, compacted FROM messages WHERE id = ?", (row_id,)).fetchone()
    assert tuple(flags) == (0, 1)
    model, display = db.get_resume_conversations("s")
    assert model == resume_before
    assert display[-1]["content"] == "rejected needle"


def test_archived_append_keeps_session_closed_guard(db):
    db.end_session("s", "compression")
    with pytest.raises(CompressionSessionClosedError):
        db.append_message("s", "user", "rejected", archived=True)
    assert db.get_messages("s", include_compacted=True) == []


def test_archived_append_keeps_turn_lease_guard(db):
    stale = f"pid={os.getpid()}:turn=stale"
    owner = f"pid={os.getpid()}:turn=owner"
    assert db.try_acquire_session_turn_lease("s", stale, ttl_seconds=5)
    db.release_session_turn_lease("s", stale)
    assert db.try_acquire_session_turn_lease("s", owner, ttl_seconds=5)
    with pytest.raises(SessionTurnLeaseLostError):
        db.append_message("s", "user", "rejected", archived=True, turn_lease_holder=stale)
    assert db.get_messages("s", include_compacted=True) == []
    db.release_session_turn_lease("s", owner)
