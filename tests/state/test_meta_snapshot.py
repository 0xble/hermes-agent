"""Related goal metadata reads must not convoy behind an unrelated writer."""

from concurrent.futures import ThreadPoolExecutor

from hermes_state import SessionDB


def test_metadata_snapshot_reads_committed_pair_while_writer_is_busy(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        assert db._wal_active
        db.set_meta("goal", "old")
        db.set_meta("revision", "1")
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db._lock:
                db._conn.execute("BEGIN IMMEDIATE")
                try:
                    cursor = db._conn.cursor()
                    db.set_meta("goal", "new", cursor=cursor)
                    db.set_meta("revision", "2", cursor=cursor)
                    read = pool.submit(db.get_meta_values, ["goal", "revision", "missing"])
                    assert read.result(timeout=5) == {
                        "goal": "old", "revision": "1", "missing": None,
                    }
                    db._conn.commit()
                finally:
                    db._conn.rollback()
            assert db.get_meta_values(["goal", "revision"]) == {
                "goal": "new", "revision": "2",
            }
    finally:
        db.close()


def test_metadata_snapshot_uses_supported_reader_fallback(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.set_meta("goal", "saved")
        monkeypatch.setattr(db, "_checkout_read_conn", lambda: None)
        assert db.get_meta_values(["goal", "missing"]) == {"goal": "saved", "missing": None}
        assert db.get_meta_values([]) == {}
    finally:
        db.close()
