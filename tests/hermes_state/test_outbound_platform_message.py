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


def test_legacy_content_match_is_newest_and_does_not_overwrite(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("session", "telegram")
        db.append_messages_batch("session", [
            {"role": "assistant", "content": "same answer"},
            {"role": "assistant", "content": "same answer"},
        ])

        assert db.record_outbound_platform_message("session", "telegram-latest", "same answer") is True
        assert db.record_outbound_platform_message("session", "telegram-older", "same answer") is True

        rows = db._conn.execute(  # type: ignore[union-attr]
            "SELECT platform_message_id FROM messages WHERE session_id = ? ORDER BY id",
            ("session",),
        ).fetchall()
    finally:
        db.close()

    assert [row[0] for row in rows] == ["telegram-older", "telegram-latest"]
