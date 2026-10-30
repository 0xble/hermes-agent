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




@pytest.mark.parametrize("first, second, first_writer, expected", [
    ("Q3 Billing", "q3 billing", "a", {"a": "Q3 Billing"}),
    ("Q3 Billing", "q3 billing", "b", {"b": "q3 billing"}),
    ("Q3 Billing #2", "Q3 Billing", "a", {"a": "Q3 Billing #2"}),
    ("Q3 Billing #2", "Q3 Billing", "b", {"a": "Q3 Billing #2", "b": "Q3 Billing"}),
    ("Straße", "STRASSE", "a", {"a": "Straße"}),
    ("Straße", "STRASSE", "b", {"b": "STRASSE"}),
])
def test_concurrent_final_titles_respect_collision_contract_after_stale_checks(
    tmp_path, first, second, first_writer, expected,
):
    """Both reads are stale, then exercise each write order without relying on scheduling.

    Only a stored alias loses its suffix. A numbered candidate after an unnumbered
    title is a distinct subject, but an unnumbered candidate after an alias collides.
    """
    import threading
    from unittest.mock import patch
    from agent.title_generator import auto_title_session

    with SessionDB(tmp_path / "state.db") as db:
        for session_id in ("a", "b"):
            db.create_session(session_id=session_id, source="telegram")
        checked = threading.Barrier(2, timeout=10)
        first_written = threading.Event()
        real_check = db.has_auto_title_collision
        real_write = db.set_auto_title
        read_results = {}

        def stale_check(title, *, exclude_session_id=None):
            result = real_check(title, exclude_session_id=exclude_session_id)
            read_results[exclude_session_id] = result
            checked.wait()
            return result

        def ordered_write(session_id, title, **kwargs):
            if session_id != first_writer:
                assert first_written.wait(10), "first title write did not finish"
            try:
                return real_write(session_id, title, **kwargs)
            finally:
                if session_id == first_writer:
                    first_written.set()

        titles = {"a": first, "b": second}
        with patch.object(db, "has_auto_title_collision", side_effect=stale_check), \
             patch.object(db, "set_auto_title", side_effect=ordered_write), \
             patch("agent.title_generator.generate_title",
                   side_effect=lambda message, **_: titles[message]):
            workers = [threading.Thread(target=auto_title_session, args=(db, sid, sid)) for sid in ("a", "b")]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
            assert all(not worker.is_alive() for worker in workers)
        assert read_results == {"a": False, "b": False}
        stored = {sid: (db.get_session_title(sid), db.get_session_title_source(sid)) for sid in ("a", "b")}
        assert stored == {sid: (expected[sid], "llm") if sid in expected else (None, None)
                          for sid in ("a", "b")}


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
