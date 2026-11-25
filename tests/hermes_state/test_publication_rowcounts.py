"""Publication row counts describe statements, excluding trigger bookkeeping."""

import pytest

from hermes_state import SessionDB
from hermes_state_publication import publication_metrics


@pytest.mark.parametrize("abort", [False, True])
def test_publication_counts_statement_rows_without_trigger_writes(tmp_path, abort):
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("s", source="cli")

        def publish(conn):
            conn.execute("UPDATE sessions SET title = 'published' WHERE id = 's'")
            conn.executemany("UPDATE sessions SET pinned = ? WHERE id = 's'", [(1,), (0,)])
            cursor = conn.cursor()
            cursor.execute("UPDATE sessions SET archived = 1 WHERE id = 's'")
            cursor.execute("SELECT title FROM sessions").fetchall()
            cursor.close()
            conn.execute("UPDATE sessions SET hidden = 1 WHERE id = 's' RETURNING id").fetchall()
            if abort:
                raise ValueError("abort publication")

        with publication_metrics(10) as metrics:
            if abort:
                with pytest.raises(ValueError, match="abort publication"):
                    db._execute_write(publish)
            else:
                db._execute_write(publish)
        assert metrics["publish_rows_written"] == 5
        assert db._conn.publication_cursors is None
        assert db.get_session_title("s") == (None if abort else "published")
