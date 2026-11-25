"""Outbound platform ids attach to the exact persisted assistant row."""

from __future__ import annotations

from hermes_state import SessionDB


def test_message_uid_selects_the_exact_duplicate_reply(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("session", "telegram")
        first = {"role": "assistant", "content": "same answer"}
        second = {"role": "assistant", "content": "same answer"}
        db.append_messages_batch("session", [first, second])

        assert db.record_outbound_platform_message(
            "session", "telegram-2", "same answer", message_uid=second["message_uid"],
        ) is True

        rows = db._conn.execute(  # type: ignore[union-attr]
            "SELECT message_uid, platform_message_id FROM messages WHERE session_id = ? ORDER BY id",
            ("session",),
        ).fetchall()
    finally:
        db.close()

    assert [tuple(row) for row in rows] == [
        (first["message_uid"], None),
        (second["message_uid"], "telegram-2"),
    ]


def test_missing_message_uid_does_not_content_match(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("session", "telegram")
        db.append_messages_batch("session", [{"role": "assistant", "content": "same answer"}])
        assert db.record_outbound_platform_message("session", "telegram-1", "same answer") is False
    finally:
        db.close()


def test_message_uid_ignores_inactive_compaction_copy(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("session", "telegram")
        row = {"role": "assistant", "content": "same answer"}
        db.append_messages_batch("session", [row])
        uid = row["message_uid"]
        db._conn.execute(  # type: ignore[union-attr]
            "UPDATE messages SET active = 0 WHERE session_id = ? AND message_uid = ?",
            ("session", uid),
        )
        db._conn.commit()  # type: ignore[union-attr]
        active = {"role": "assistant", "content": "same answer", "message_uid": uid}
        db.append_messages_batch("session", [active])

        assert db.record_outbound_platform_message(
            "session", "telegram-active", "different delivered text", message_uid=uid,
        ) is True
        rows = db._conn.execute(  # type: ignore[union-attr]
            "SELECT active, platform_message_id FROM messages WHERE session_id = ? ORDER BY id",
            ("session",),
        ).fetchall()
    finally:
        db.close()

    assert [tuple(row) for row in rows] == [(0, None), (1, "telegram-active")]
