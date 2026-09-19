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
