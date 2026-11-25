import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def test_lineage_title_reservation_uses_next_alias_atomically(db):
    db.create_session("one", source="telegram")
    db.create_session("two", source="telegram")
    assert db.set_session_title_in_lineage("one", "Shared topic") == "Shared topic"
    assert db.set_session_title_in_lineage("two", "Shared topic") == "Shared topic #2"
    assert db.get_session("one")["title"] == "Shared topic"
    assert db.get_session("two")["title"] == "Shared topic #2"


def test_icon_history_is_profile_scoped_and_bounded(db):
    db.apply_telegram_topic_migration()
    for index in range(14):
        db.record_telegram_topic_icon_history(
            "chat", emoji=f"e{index}", custom_emoji_id=str(index), profile_name="profile-a"
        )
    db.record_telegram_topic_icon_history("chat", emoji="other", profile_name="profile-b")

    assert db.list_recent_telegram_topic_icons("chat", 24, "profile-a") == [
        *(f"e{index}" for index in range(13, 1, -1))
    ]
    assert db.list_recent_telegram_topic_icons("chat", 24, "profile-b") == ["other"]


def test_user_typed_numbered_title_is_kept_when_free(db):
    db.create_session("a", source="telegram")
    assert db.set_session_title_in_lineage("a", "Sprint #3") == "Sprint #3"
    assert db.get_session("a")["title"] == "Sprint #3"
    assert db.get_session_title_source("a") == SessionDB.TITLE_SOURCE_USER


def test_lineage_alias_respects_canonical_bot_chat_guard(db):
    db.create_session("bot", source="cli")
    db.set_session_title("bot", SessionDB.CANONICAL_BOT_CHAT_TITLE)
    db._write_sql("UPDATE sessions SET hidden = 1 WHERE id = ?", ("bot",))
    with pytest.raises(ValueError, match="canonical Bot Chat"):
        db.set_session_title_in_lineage("bot", "Renamed")


def test_legacy_archived_fork_icon_tables_are_rebuilt(tmp_path):
    """The archived fork left ``ownership``/``observed_at`` state rows and a NOT NULL
    ``custom_emoji_id`` history PK. Migration must rebuild both so writes succeed, keeping
    manual ownership and collapsing ``default`` to ``auto``."""
    import sqlite3

    path = tmp_path / "state.db"
    seed = SessionDB(path)
    seed.close()
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE telegram_topic_icon_state (
                profile_name TEXT NOT NULL DEFAULT 'default', chat_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                custom_emoji_id TEXT,
                ownership TEXT NOT NULL CHECK (ownership IN ('auto','manual','default')),
                observed_at REAL NOT NULL, PRIMARY KEY (profile_name, chat_id, thread_id));
            INSERT INTO telegram_topic_icon_state VALUES
                ('default','c','1','m-id','manual',1.0), ('default','c','2','a-id','auto',1.0),
                ('default','c','3',NULL,'default',1.0);
            CREATE TABLE telegram_topic_icon_history (
                profile_name TEXT NOT NULL DEFAULT 'default', chat_id TEXT NOT NULL,
                custom_emoji_id TEXT NOT NULL, emoji TEXT NOT NULL, selected_at REAL NOT NULL,
                PRIMARY KEY (profile_name, chat_id, custom_emoji_id));
        """)

    db = SessionDB(path)
    db.apply_telegram_topic_migration()
    assert db.get_telegram_topic_icon_state("c", "1")["owner"] == "manual"
    assert db.get_telegram_topic_icon_state("c", "3")["owner"] == "auto"
    db.record_telegram_topic_icon_history("c", emoji="x")  # custom_emoji_id=None must be accepted
    db.record_telegram_topic_icon_history("c", emoji="x")  # and duplicates are not a PK conflict
    assert db.list_recent_telegram_topic_icons("c") == ["x", "x"]
    db.record_telegram_topic_icon_state("c", "9", emoji="y", owner="auto")
    assert db.get_telegram_topic_icon_state("c", "9")["emoji"] == "y"
