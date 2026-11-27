"""Legacy icon migration must roll back with the rest of the topic schema."""

import sqlite3

import pytest

from hermes_state import SessionDB


def _legacy_icons(conn):
    conn.executescript("""
        CREATE TABLE telegram_topic_icon_state (
            profile_name TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id TEXT NOT NULL,
            custom_emoji_id TEXT, ownership TEXT NOT NULL, observed_at REAL NOT NULL,
            PRIMARY KEY (profile_name, chat_id, thread_id)
        );
        INSERT INTO telegram_topic_icon_state VALUES
            ('default', 'chat', '1', 'manual-icon', 'manual', 1.0),
            ('other', 'chat', '2', 'auto-icon', 'auto', 2.0),
            ('default', 'chat', '3', NULL, 'default', 3.0);
        CREATE TABLE telegram_topic_icon_history (
            profile_name TEXT NOT NULL, chat_id TEXT NOT NULL, emoji TEXT NOT NULL,
            custom_emoji_id TEXT NOT NULL, selected_at REAL NOT NULL,
            PRIMARY KEY (profile_name, chat_id, custom_emoji_id)
        );
        INSERT INTO telegram_topic_icon_history VALUES ('default', 'chat', 'old', 'old-icon', 1.0);
    """)


def _topic_snapshot(conn):
    schema = [tuple(row) for row in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE name LIKE 'telegram_%' OR name LIKE 'idx_telegram_%' ORDER BY name"
    )]
    data = {name: [tuple(row) for row in conn.execute(f'SELECT * FROM {name}')]
            for name, _ in schema if not name.startswith('idx_')}
    return schema, data


@pytest.mark.parametrize("legacy, action, target", [
    (True, sqlite3.SQLITE_DROP_TABLE, "telegram_topic_icon_state"),
    (True, sqlite3.SQLITE_DROP_TABLE, "telegram_topic_icon_history"),
    (True, sqlite3.SQLITE_CREATE_INDEX, "idx_telegram_dm_topic_bindings_user"),
    (True, sqlite3.SQLITE_INSERT, "state_meta"),
    (False, sqlite3.SQLITE_CREATE_INDEX, "idx_telegram_dm_topic_bindings_user"),
])
def test_topic_migration_failure_preserves_schema_rows_and_retries(tmp_path, legacy, action, target):
    db = SessionDB(tmp_path / "state.db")
    try:
        if legacy:
            _legacy_icons(db._conn)
        before = _topic_snapshot(db._conn)
        transactions = []

        def deny_stage(operation, name, *unused):
            if operation == action and name == target:
                transactions.append(db._conn.in_transaction)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db._conn.set_authorizer(deny_stage)
        try:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                db.apply_telegram_topic_migration()
        finally:
            db._conn.set_authorizer(None)
        assert _topic_snapshot(db._conn) == before
        assert db.get_meta("telegram_dm_topic_schema_version") is None
        assert transactions and all(transactions)
        assert not db._conn.in_transaction

        db.apply_telegram_topic_migration()
        db.apply_telegram_topic_migration()
        assert db.get_meta("telegram_dm_topic_schema_version") == "4"
        if legacy:
            manual = db.get_telegram_topic_icon_state("chat", "1")
            assert (manual["owner"], manual["custom_emoji_id"], manual["updated_at"]) == ("manual", "manual-icon", 1.0)
            assert db.get_telegram_topic_icon_state("chat", "2", profile_name="other")["owner"] == "auto"
            assert db.get_telegram_topic_icon_state("chat", "2") is None
            assert db.get_telegram_topic_icon_state("chat", "3")["owner"] == "auto"
        assert db.list_recent_telegram_topic_icons("chat") == []
        db.record_telegram_topic_icon_history("chat", emoji="new")
        assert db.list_recent_telegram_topic_icons("chat") == ["new"]
    finally:
        db.close()
