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
