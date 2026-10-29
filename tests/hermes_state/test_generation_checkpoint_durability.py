"""Generation fencing must survive a blocked first checkpoint (regression for #494)."""

import sqlite3
from contextlib import ExitStack, closing

import pytest

from hermes_state import SessionDB
from hermes_state_dbfile import _pread_db_range
from hermes_state_errors import _STATE_DB_GENERATION_KEY


def _disk_generation(db_path, snapshot_path):
    # Closing a new descriptor on the live DB would cancel this process's POSIX
    # SQLite locks. Copy through the existing lock-safe probe, without its WAL.
    contents = _pread_db_range(db_path, 0, db_path.stat().st_size)
    assert contents is not None
    snapshot_path.write_bytes(contents)
    application_id = int.from_bytes(contents[68:72], "big")
    with closing(sqlite3.connect(f"{snapshot_path.as_uri()}?immutable=1", uri=True)) as disk:
        row = disk.execute(
            "SELECT value FROM state_meta WHERE key = ?", (_STATE_DB_GENERATION_KEY,)
        ).fetchone()
    return application_id, row[0] if row else None


@pytest.mark.requires_wal
def test_warm_open_makes_generation_durable_after_reader_releases(tmp_path):
    db_path = tmp_path / "state.db"
    snapshot_path = tmp_path / "main-only.db"
    # Keep schema setup out of the disputed first generation-stamping open.
    with closing(SessionDB(db_path=db_path)):
        pass

    with ExitStack() as handles:
        keeper = handles.enter_context(closing(sqlite3.connect(db_path, isolation_level=None)))
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("DELETE FROM state_meta WHERE key = ?", (_STATE_DB_GENERATION_KEY,))
        keeper.execute("PRAGMA application_id=0")
        keeper.execute("PRAGMA wal_checkpoint(PASSIVE)")
        assert _disk_generation(db_path, snapshot_path) == (0, None)

        # Give the reader a WAL end mark older than the generation's frames.
        keeper.execute("INSERT INTO state_meta (key, value) VALUES ('snapshot', 'old')")
        reader = handles.enter_context(closing(sqlite3.connect(db_path, isolation_level=None)))
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM state_meta").fetchone()

        first = handles.enter_context(closing(SessionDB(db_path=db_path)))
        token = keeper.execute(
            "SELECT value FROM state_meta WHERE key = ?", (_STATE_DB_GENERATION_KEY,)
        ).fetchone()[0]
        application_id = keeper.execute("PRAGMA application_id").fetchone()[0]
        assert token and application_id
        assert _disk_generation(db_path, snapshot_path) == (0, None)

        reader.execute("ROLLBACK")
        # No close() or periodic checkpoint may repair the stamp for this open.
        handles.enter_context(closing(SessionDB(db_path=db_path)))
        assert _disk_generation(db_path, snapshot_path) == (application_id, token)
        assert first._db_file_application_id == application_id
