import gzip
import json
import sqlite3
import signal
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

import pytest

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
    assert archived[0]["_format"] == "hermes-compacted-history-v1"
    archived = archived[1:]
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
        assert [json.loads(line)["id"] for line in f if '"id"' in line] == [1, 2]


def test_crash_after_archive_append_recovers_without_duplicates(tmp_path, monkeypatch):
    path = _fixture(tmp_path)
    archive = tmp_path / "history.jsonl.gz"
    db = SessionDB(db_path=path)
    original = db._execute_write
    calls = 0

    def fail_once(fn, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected crash")
        return original(fn, **kwargs)

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
        archived_ids = [json.loads(line)["id"] for line in f if '"id"' in line]
    subprocess.run(["gunzip", "-t", str(archive)], check=True)
    assert archived_ids == [1, 2]
    assert len(archived_ids) == len(set(archived_ids))


def test_foreign_archive_is_refused_without_deleting(tmp_path):
    path = _fixture(tmp_path)
    foreign_path = tmp_path / "foreign.db"
    foreign_archive = tmp_path / "history.jsonl.gz"
    foreign = SessionDB(db_path=foreign_path)
    sid = foreign.create_session(session_id="foreign", source="cli")
    foreign.append_message(sid, role="user", content="foreign")
    foreign.close()
    foreign = SessionDB(db_path=foreign_path)
    foreign.compacted_history_retention(archive_path=foreign_archive)
    foreign.close()

    before = _rows(path)
    db = SessionDB(db_path=path)
    try:
        with pytest.raises(ValueError, match="different database"):
            db.compacted_history_retention(archive_path=foreign_archive)
    finally:
        db.close()
    assert _rows(path) == before


def test_archive_identity_mismatch_is_error(tmp_path):
    path = _fixture(tmp_path)
    archive = tmp_path / "history.jsonl.gz"
    from hermes_state_maintenance import _append_compacted_archive_member, _compacted_archive_identity
    db = SessionDB(db_path=path)
    identity = _compacted_archive_identity(db)
    _append_compacted_archive_member(archive, [{"_format": "hermes-compacted-history-v1", "identity": identity}])
    _append_compacted_archive_member(archive, [{"id": 1, "session_id": "wrong", "timestamp": -1}])
    db.close()
    before = _rows(path)
    db = SessionDB(db_path=path)
    try:
        with pytest.raises(ValueError, match="conflicting message identity"):
            db.compacted_history_retention(archive_path=archive)
    finally:
        db.close()
    assert _rows(path) == before


def test_foreign_archive_with_torn_tail_is_byte_identical(tmp_path):
    path = _fixture(tmp_path)
    foreign_path = tmp_path / "foreign.db"
    archive = tmp_path / "history.jsonl.gz"
    foreign = SessionDB(db_path=foreign_path)
    sid = foreign.create_session(session_id="foreign", source="cli")
    foreign.append_message(sid, role="user", content="foreign")
    foreign.close()
    foreign = SessionDB(db_path=foreign_path)
    from hermes_state_maintenance import _append_compacted_archive_member
    _append_compacted_archive_member(archive, [{"_format": "hermes-compacted-history-v1",
                                                "identity": {"db_path": str(foreign_path.resolve())}}])
    _append_compacted_archive_member(archive, [{"id": 99, "session_id": "foreign", "timestamp": 1}])
    foreign.close()
    data = archive.read_bytes()
    archive.write_bytes(data[:-5])
    before = archive.read_bytes()
    db = SessionDB(db_path=path)
    try:
        with pytest.raises(ValueError, match="different database"):
            db.compacted_history_retention(archive_path=archive)
    finally:
        db.close()
    assert archive.read_bytes() == before


def test_empty_and_torn_first_header_are_rebuilt(tmp_path):
    from hermes_state_maintenance import _append_compacted_archive_member
    for torn in (False, True):
        path = _fixture(tmp_path / ("torn" if torn else "empty"))
        archive = path.parent / "history.jsonl.gz"
        if torn:
            _append_compacted_archive_member(
                archive, [{"_format": "hermes-compacted-history-v1", "identity": {"db_path": "old"}}]
            )
            archive.write_bytes(archive.read_bytes()[:-5])
        else:
            archive.write_bytes(b"")
        db = SessionDB(db_path=path)
        result = db.compacted_history_retention(archive_path=archive)
        db.close()
        assert result["deleted"] == 2
        subprocess.run(["gunzip", "-t", str(archive)], check=True)
        with gzip.open(archive, "rt", encoding="utf-8") as stream:
            assert json.loads(next(stream))["_format"] == "hermes-compacted-history-v1"


def test_archive_reader_retains_only_message_identity_index(tmp_path):
    from hermes_state_maintenance import _read_compacted_archive
    archive = tmp_path / "large.jsonl.gz"
    with gzip.open(archive, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps({"_format": "hermes-compacted-history-v1",
                                 "identity": {"db_path": "synthetic"}}) + "\n")
        body = "x" * 2048
        for message_id in range(200_000):
            stream.write(json.dumps({"id": message_id, "session_id": "s", "timestamp": message_id,
                                     "content": body}) + "\n")
    tracemalloc.start()
    try:
        identity, archived, recovered, offset = _read_compacted_archive(archive)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert identity == {"db_path": "synthetic"}
    assert not recovered
    assert offset == archive.stat().st_size
    assert len(archived) == 200_000
    assert all(len(key) == 3 for key in archived.values())
    assert peak < 96 * 1024 * 1024


def test_zero_delete_progress_fails_instead_of_spinning(tmp_path, monkeypatch):
    path = _fixture(tmp_path)
    db = SessionDB(db_path=path)
    monkeypatch.setattr(db, "_execute_write", lambda fn, **kwargs: 0)
    with pytest.raises(RuntimeError, match="made no delete progress"):
        db.compacted_history_retention(archive_path=tmp_path / "history.jsonl.gz")
    db.close()


def test_real_sigkill_after_archive_write_recovers(tmp_path):
    path = _fixture(tmp_path)
    archive = tmp_path / "history.jsonl.gz"
    code = """
import os, signal, sys
from pathlib import Path
from hermes_state import SessionDB
import hermes_state_maintenance as maintenance
original = maintenance._append_compacted_archive_member
calls = [0]
def crash(*args, **kwargs):
    original(*args, **kwargs)
    calls[0] += 1
    if calls[0] == 2:
        os.kill(os.getpid(), signal.SIGKILL)
maintenance._append_compacted_archive_member = crash
db = SessionDB(db_path=Path(sys.argv[1]))
db.compacted_history_retention(archive_path=Path(sys.argv[2]), batch_size=2)
"""
    child = subprocess.run([sys.executable, "-c", code, str(path), str(archive)], check=False)
    assert child.returncode == -signal.SIGKILL
    db = SessionDB(db_path=path)
    assert db.compacted_history_retention(archive_path=archive, batch_size=2)["deleted"] == 2
    db.close()
    subprocess.run(["gunzip", "-t", str(archive)], check=True)
    assert [row[0] for row in _rows(path)] == [3, 4]


def test_real_sigkill_after_delete_is_idempotent(tmp_path):
    path = _fixture(tmp_path)
    archive = tmp_path / "history.jsonl.gz"
    code = """
import os, signal, sys
from pathlib import Path
from hermes_state import SessionDB
path, archive = map(Path, sys.argv[1:])
db = SessionDB(db_path=path)
original = db._execute_write
def crash(*args, **kwargs):
    result = original(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
    return result
db._execute_write = crash
db.compacted_history_retention(archive_path=archive, batch_size=2)
"""
    child = subprocess.run([sys.executable, "-c", code, str(path), str(archive)], check=False)
    assert child.returncode == -signal.SIGKILL
    db = SessionDB(db_path=path)
    assert db.compacted_history_retention(archive_path=archive, batch_size=2)["deleted"] == 0
    db.close()
    subprocess.run(["gunzip", "-t", str(archive)], check=True)
    assert [row[0] for row in _rows(path)] == [3, 4]


def test_truncated_trailing_member_is_repaired(tmp_path):
    path = _fixture(tmp_path)
    archive = tmp_path / "history.jsonl.gz"
    from hermes_state_maintenance import (
        _append_compacted_archive_member,
        _compacted_archive_identity,
    )
    db = SessionDB(db_path=path)
    _append_compacted_archive_member(
        archive, [{"_format": "hermes-compacted-history-v1", "identity": _compacted_archive_identity(db)}]
    )
    row = dict(db._read_all("SELECT * FROM messages WHERE active=0 ORDER BY id LIMIT 1")[0])
    _append_compacted_archive_member(archive, [row])
    db.close()
    data = archive.read_bytes()
    archive.write_bytes(data[:-5])
    db = SessionDB(db_path=path)
    result = db.compacted_history_retention(archive_path=archive, batch_size=2)
    db.close()
    assert result["recovered_truncated_member"] is True
    assert result["deleted"] == 2
    subprocess.run(["gunzip", "-t", str(archive)], check=True)
    assert [row[0] for row in _rows(path)] == [3, 4]
