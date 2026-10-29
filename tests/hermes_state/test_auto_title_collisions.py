"""Automatic title rejection does not change manual title authority or numeric subjects."""

import pytest

from hermes_state import SessionDB


@pytest.mark.parametrize("existing,candidate,expected", [
    ("INFLUENCE AND PERSUASION #2", "Influence and Persuasion", True),
    ("Review PR #586", "Review PR #597", False),
    ("Review PR #586", "review pr #586", True),
    ("Review PR #586 #2", "Review PR #586", True),
    ("Influence and Persuasion", "Influence and Persuasion #3", False),
    ("Straße #2", "STRASSE", True),
    ("100%_Ready #2", "100%_Ready", True),
    ("100XXReady #2", "100%_Ready", False),
    ("Research #two", "Research", False),
])
def test_auto_title_collision_lookup(tmp_path, existing, candidate, expected):
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(session_id="taken", source="telegram")
        db.set_session_title("taken", existing)
        db.create_session(session_id="active", source="telegram")
        assert db.has_auto_title_collision(candidate, exclude_session_id="active") is expected
        assert db.has_auto_title_collision(candidate, exclude_session_id="taken") is False


def test_manual_titles_can_still_share_a_subject(tmp_path):
    with SessionDB(tmp_path / "state.db") as db:
        for session_id in ("first", "second"):
            db.create_session(session_id=session_id, source="telegram")
            db.set_session_title_in_lineage(session_id, "Shared Manual Subject")
        assert db.get_session_title("first") == "Shared Manual Subject"
        assert db.get_session_title("second") == "Shared Manual Subject #2"
        assert db.get_session_title_source("second") == "user"
