import gzip
import json
import sqlite3
import subprocess
import time
from pathlib import Path

from hermes_state import SessionDB


def _fixture(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    sid = db.create_session(session_id="s1", source="cli")
    for i in range(4):
        db.append_message(sid, role="user", content=f"needle-{i}")
    db.close()
    old = time.time() - 30 * 86400
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE messages SET active=0, timestamp=? WHERE id IN (1, 2)", (old,))
    conn.execute("UPDATE messages SET timestamp=? WHERE id=3", (old,))
    conn.commit()
    conn.close()
    return db_path


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT id, active, content FROM messages ORDER BY id").fetchall()
    finally:
        conn.close()


def test_round_trip_filters_and_fts(tmp_path):
    path = _fixture(tmp_path)
    archive = tmp_path / "history.jsonl.gz"
    db = SessionDB(db_path=path)
    result = db.compacted_history_retention(older_than_days=14, archive_path=archive)
    db.close()
    assert result["deleted"] == 2
    with gzip.open(archive, "rt", encoding="utf-8") as f:
        archived = [json.loads(line) for line in f]
    assert {row["id"] for row in archived} == {1, 2}
    assert {"id", "session_id", "role", "content", "timestamp", "active"} <= archived[0].keys()
    assert _rows(path) == [(3, 1, "needle-2"), (4, 1, "needle-3")]
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM messages_fts_trigram").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM messages_fts WHERE rowid IN (1, 2)").fetchone()[0] == 0
    finally:
        conn.close()


def test_dry_run_does_not_write(tmp_path):
    path = _fixture(tmp_path)
    archive = tmp_path / "history.jsonl.gz"
    before = _rows(path)
    db = SessionDB(db_path=path)
    result = db.compacted_history_retention(archive_path=archive, dry_run=True)
    db.close()
    assert result["rows"] == 2
    assert not archive.exists()
    assert _rows(path) == before


def test_existing_archive_is_preserved_and_rerun_is_idempotent(tmp_path):
    path = _fixture(tmp_path)
    archive = tmp_path / "history.jsonl.gz"
    db = SessionDB(db_path=path)
    db.compacted_history_retention(archive_path=archive)
    db.close()
    first = archive.read_bytes()
    db = SessionDB(db_path=path)
    result = db.compacted_history_retention(archive_path=archive)
    db.close()
    assert result["deleted"] == 0
    assert archive.read_bytes() == first
    with gzip.open(archive, "rt", encoding="utf-8") as f:
        assert [json.loads(line)["id"] for line in f] == [1, 2]


def test_crash_after_archive_append_recovers_without_duplicates(tmp_path, monkeypatch):
    path = _fixture(tmp_path)
    archive = tmp_path / "history.jsonl.gz"
    db = SessionDB(db_path=path)
    original = db._execute_write
    calls = 0

    def fail_once(fn):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected crash")
        return original(fn)

    monkeypatch.setattr(db, "_execute_write", fail_once)
    try:
        db.compacted_history_retention(archive_path=archive)
    except RuntimeError:
        pass
    db.close()
    db = SessionDB(db_path=path)
    assert db.compacted_history_retention(archive_path=archive)["deleted"] == 2
    db.close()
    with gzip.open(archive, "rt", encoding="utf-8") as f:
        archived_ids = [json.loads(line)["id"] for line in f]
    subprocess.run(["gunzip", "-t", str(archive)], check=True)
    assert archived_ids == [1, 2]
    assert len(archived_ids) == len(set(archived_ids))
    assert [row[0] for row in _rows(path)] == [3, 4]
