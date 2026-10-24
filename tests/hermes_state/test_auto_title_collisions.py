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


@pytest.mark.parametrize("first, second", [
    ("Q3 Billing", "q3 billing"),
    ("Q3 Billing #2", "Q3 Billing"),
    ("Straße", "STRASSE"),
])
def test_concurrent_final_titles_cannot_both_pass_a_stale_collision_check(tmp_path, first, second):
    """Both workers pass the read-only check before either writes; the write decides."""
    import threading
    from unittest.mock import patch
    from agent.title_generator import auto_title_session

    with SessionDB(tmp_path / "state.db") as db:
        for session_id in ("a", "b"):
            db.create_session(session_id=session_id, source="telegram")
        checked = threading.Barrier(2, timeout=10)
        real_check = db.has_auto_title_collision

        def stale_check(title, *, exclude_session_id=None):
            result = real_check(title, exclude_session_id=exclude_session_id)
            checked.wait()
            return result

        titles = {"a": first, "b": second}
        with patch.object(db, "has_auto_title_collision", side_effect=stale_check), \
             patch("agent.title_generator.generate_title",
                   side_effect=lambda message, **_: titles[message]):
            workers = [threading.Thread(target=auto_title_session, args=(db, sid, sid)) for sid in ("a", "b")]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
        stored = {sid: (db.get_session_title(sid), db.get_session_title_source(sid)) for sid in ("a", "b")}
        finals = [value for value in stored.values() if value[1] == "llm"]
        assert len(finals) == 1, stored
        assert finals[0][0] in (first, second)


def test_atomic_collision_rejection_never_blocks_a_manual_title(tmp_path):
    with SessionDB(tmp_path / "state.db") as db:
        for session_id in ("a", "b"):
            db.create_session(session_id=session_id, source="telegram")
        assert db.set_auto_title("a", "Q3 Billing", source="llm", reject_collision=True)
        assert not db.set_auto_title("b", "q3 billing", source="llm", reject_collision=True)
        assert db.get_session_title("b") is None
        db.set_session_title("b", "q3 billing")
        assert db.get_session_title("b") == "q3 billing"
        assert db.get_session_title_source("b") == "user"
