"""Retiring fork commands must preserve native commands and saved history."""

import sqlite3

import pytest

from hermes_cli.commands import is_gateway_known_command, resolve_command
from hermes_state import SessionDB


@pytest.mark.parametrize("name", ["side", "spawn", "merge", "fold"])
def test_retired_commands_are_not_dispatched(name):
    assert resolve_command(name) is None
    assert not is_gateway_known_command(name)


@pytest.mark.parametrize("name", ["bg", "branch", "btw", "resume"])
def test_native_session_commands_remain_available(name):
    assert resolve_command(name) is not None
    assert is_gateway_known_command(name)


def test_fresh_database_does_not_create_side_bookkeeping(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    try:
        with sqlite3.connect(path) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert "side_message_bindings" not in tables
        assert "session_context_merges" not in tables
    finally:
        db.close()


def test_existing_side_history_and_receipts_survive_reopen(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    try:
        db.create_session(session_id="parent", source="telegram")
        db.create_session(session_id="child", source="telegram", parent_session_id="parent")
        child_message = db.append_message(
            session_id="child", role="assistant", content="Historical side answer"
        )
        receipt = db.append_message(
            session_id="parent", role="user", content="Historical merged context receipt"
        )
    finally:
        db.close()

    # Reproduce the removed schema, including its foreign keys, without
    # opening or mutating a real profile database.
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS side_message_bindings (
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                thread_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL,
                side_route_key TEXT NOT NULL,
                side_root_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                created_at REAL NOT NULL,
                PRIMARY KEY (platform, chat_id, thread_id, user_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS session_context_merges (
                destination_root_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                destination_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                side_root_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                side_tip_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                previous_cutoff_message_id INTEGER NOT NULL DEFAULT 0,
                source_cutoff_message_id INTEGER NOT NULL,
                receipt_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                created_at REAL NOT NULL,
                PRIMARY KEY (destination_root_session_id, side_root_session_id, source_cutoff_message_id)
            );
        """)
        conn.execute(
            "INSERT INTO side_message_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("telegram", "chat", "topic", "user", "message", "route:side:child", "child", 1),
        )
        conn.execute(
            "INSERT INTO session_context_merges VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("parent", "parent", "child", "child", 0, child_message, receipt, 1),
        )

    reopened = SessionDB(db_path=path)
    try:
        child = reopened.get_session("child")
        assert child is not None
        assert child["parent_session_id"] == "parent"
        assert reopened.get_messages("child")[0]["content"] == "Historical side answer"
        assert reopened.get_messages("parent")[0]["content"] == "Historical merged context receipt"
        reopened.append_message(session_id="parent", role="assistant", content="Normal follow-up")
        assert reopened.get_messages("parent")[-1]["content"] == "Normal follow-up"
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM side_message_bindings").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM session_context_merges").fetchone()[0] == 1
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        reopened.close()


def test_gateway_peer_fallback_ignores_legacy_spawn_children(tmp_path):
    """An unkeyed historical /spawn child must not hijack the parent's routing lane."""
    db = SessionDB(db_path=tmp_path / "legacy-recovery.db")
    peer = {
        "user_id": "user-1",
        "chat_id": "chat-1",
        "chat_type": "dm",
        "thread_id": "42",
    }
    db.create_session(
        "gw-parent",
        "telegram",
        session_key="agent:main:telegram:dm:chat-1:42",
        **peer,
    )
    db.append_message("gw-parent", "user", "parent context")
    db.create_session(
        "spawn-child",
        "telegram",
        model_config={
            "_branched_from": "gw-parent",
            "_spawned_from": "gw-parent",
        },
        parent_session_id="gw-parent",
        **peer,
    )
    db.append_message("spawn-child", "user", "spawn work")
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE sessions SET last_activity_at = 100 WHERE id = 'gw-parent'")
        conn.execute("UPDATE sessions SET last_activity_at = 200 WHERE id = 'spawn-child'")

    recovered = db.find_latest_gateway_session_for_peer(
        source="telegram",
        user_id="user-1",
        session_key="stale-or-missing-key",
        chat_id="chat-1",
        chat_type="dm",
        thread_id="42",
    )

    db.close()
    assert recovered is not None
    assert recovered["id"] == "gw-parent"
