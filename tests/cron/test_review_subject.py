"""Resolve reviewed report topics only from current, unique bindings."""
import pytest

from cron.scheduler_review import _resolve_review_subject
from hermes_state import SessionDB


@pytest.fixture
def subjects(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.apply_telegram_topic_migration()
    # Use the production schema, including topic binding migrations.
    def add(sid, thread, title, **flags):
        db.create_session(sid, source="telegram")
        with db._conn:
            db._conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, sid))
            for name, value in flags.items():
                db._conn.execute(f"UPDATE sessions SET {name}=? WHERE id=?", (value, sid))
            db._conn.execute(
                "INSERT INTO telegram_dm_topic_bindings "
                "(profile_name, chat_id, thread_id, user_id, session_key, session_id, linked_at, updated_at) "
                "VALUES ('default', 'chat', ?, 'user', ?, ?, 1, 1)", (thread, f"key-{sid}", sid))
    yield add
    db.close()


def test_exact_thread_and_title_match(subjects):
    subjects("stripe", "139328", "Stripe Priorities")
    for subject in ("139328", "Stripe Priorities"):
        assert _resolve_review_subject({"subject": subject})["session_id"] == "stripe"
    with pytest.raises(ValueError, match="0 live topics"):
        _resolve_review_subject({"subject": "Stripe"})


def test_ambiguous_thread_or_title_is_refused(subjects):
    subjects("a", "1", "First")
    subjects("b", "2", "1")
    with pytest.raises(ValueError, match="2 live topics"):
        _resolve_review_subject({"subject": "1"})


@pytest.mark.parametrize("thread, flags", [
    ("3", {"ended_at": 1}),
    ("4", {"end_reason": "merged"}), ("5", {"archived": 1}), ("6", {"hidden": 1}),
])
def test_ineligible_topics_are_excluded(subjects, thread, flags):
    subjects("excluded", thread, "Excluded", **flags)
    for subject in (thread, "Excluded"):
        with pytest.raises(ValueError, match="0 live topics"):
            _resolve_review_subject({"subject": subject})


def test_excluded_threads_are_loaded_from_profile_config_and_default_empty(subjects, tmp_path):
    subjects("topic", "255600", "Report Topic")
    assert _resolve_review_subject({"subject": "255600"})["session_id"] == "topic"
    (tmp_path / "config.yaml").write_text("cron:\n  review:\n    excluded_threads: [255600]\n")
    for subject in ("255600", "Report Topic"):
        with pytest.raises(ValueError, match="0 live topics"):
            _resolve_review_subject({"subject": subject})
