"""Regression coverage for keeping FTS5 segment merges off message writes."""

from hermes_state import SessionDB


def test_fts_automerge_is_disabled_for_writer_transactions(tmp_path):
    """Routine message inserts leave segment merging to bounded maintenance."""
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session(session_id="s1", source="cli")
        present = [table for table in db._FTS_TABLES if db._fts_table_exists(table)]
        assert len(present) >= 2
        for table in present:
            value = db._read_one(f"SELECT v FROM {table}_config WHERE k = 'automerge'")[0]
            assert str(value) == "0"
    finally:
        db.close()
