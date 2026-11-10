from __future__ import annotations

import builtins
import itertools
import math
import sqlite3

import pytest
import time

from tui_gateway import server
from hermes_state_session_signature import SESSION_SIGNATURE_FIELDS


def _seed_store(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            last_activity_at REAL,
            message_count INTEGER,
            archived INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0
        );
        CREATE TABLE gateway_heartbeats (
            backend_id TEXT PRIMARY KEY,
            last_heartbeat REAL
        );
        INSERT INTO sessions(id, title, last_activity_at, message_count)
        VALUES ('session-1', 'Original', 100, 1);
        INSERT INTO gateway_heartbeats(backend_id, last_heartbeat)
        VALUES ('backend-1', 100);
        """
    )
    conn.commit()
    conn.close()


def _seed_signature_store(path, journal):
    if not journal:
        _seed_store(path)
        return
    from hermes_state import SessionDB

    with SessionDB(path) as db:
        db.create_session("session-1", source="cli")


def test_sessions_signature_ignores_gateway_heartbeat_only_writes(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _seed_store(db_path)
    monkeypatch.setattr(server, "_watcher_home", lambda: tmp_path)
    monkeypatch.setattr(server, "_served_profile_homes", [])

    before = server._sessions_sig()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE gateway_heartbeats SET last_heartbeat = 200")
    conn.commit()
    conn.close()

    assert server._sessions_sig() == before


def test_sessions_signature_changes_for_session_metadata_updates(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _seed_store(db_path)
    monkeypatch.setattr(server, "_watcher_home", lambda: tmp_path)
    monkeypatch.setattr(server, "_served_profile_homes", [])

    before = server._sessions_sig()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE sessions SET title = 'Renamed' WHERE id = 'session-1'")
    conn.commit()
    conn.close()

    assert server._sessions_sig() != before


def test_sessions_signature_sees_a_commit_inside_the_same_mtime_tick(tmp_path, monkeypatch):
    """Filesystem timestamps tick coarsely (~4 ms; whole seconds on some filesystems), and SQLite
    commits in place: a commit in the tick of the previous read leaves the DB/WAL mtime unchanged."""
    db_path = tmp_path / "state.db"
    _seed_store(db_path)
    monkeypatch.setattr(server, "_watcher_home", lambda: tmp_path)
    monkeypatch.setattr(server, "_served_profile_homes", [])
    frozen_mtime = time.time_ns()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda _paths: frozen_mtime)

    before = server._sessions_sig()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE sessions SET title = 'Renamed' WHERE id = 'session-1'")
    conn.commit()
    conn.close()

    assert server._sessions_sig() != before


def test_watcher_refreshes_do_not_format_session_rows(tmp_path, monkeypatch):
    """A busy sibling WAL must not repeatedly stringify every floating-point timestamp."""
    db_path = tmp_path / "state.db"
    _seed_store(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE sessions ADD COLUMN started_at REAL")
    conn.execute("UPDATE sessions SET started_at = 1800000000.1234567")
    conn.commit()
    conn.close()
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    formatted_rows = []

    def counted_repr(value):
        formatted_rows.append(value)
        return builtins.repr(value)

    monkeypatch.setattr(server, "repr", counted_repr, raising=False)
    signatures = [server._session_db_content_sig(db_path) for _ in range(5)]
    assert all(sig == signatures[0] for sig in signatures)
    assert signatures[0][0] != "mtime-fallback"
    assert len(formatted_rows) == 0, "watcher formatted rows on every dirty-WAL tick"


@pytest.mark.parametrize("journal", [False, True])
@pytest.mark.parametrize("before_value, after_value", [
    (1800000000.1234567, math.nextafter(1800000000.1234567, math.inf)),
    (None, "None"),
    ("a\0b", "a\0c"),
    ("é\n'\\", "é\r'\\"),
    (b"text", "text"),
])
def test_session_signature_keeps_exact_sqlite_values(tmp_path, monkeypatch, before_value, after_value, journal):
    db_path = tmp_path / "state.db"
    _seed_signature_store(db_path, journal)
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    conn = sqlite3.connect(db_path)
    field = "started_at" if isinstance(before_value, float) else "title"
    if field == "started_at" and not journal:
        conn.execute("ALTER TABLE sessions ADD COLUMN started_at REAL")
    conn.execute(f"UPDATE sessions SET {field} = ?", (before_value,))
    conn.commit()
    before = server._session_db_content_sig(db_path)
    assert before == server._session_db_content_sig(db_path)
    conn.execute(f"UPDATE sessions SET {field} = ?", (after_value,))
    conn.commit()
    after = server._session_db_content_sig(db_path)
    conn.close()
    assert before != after
    assert before[0] != "mtime-fallback"
    assert after[0] != "mtime-fallback"


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("field", SESSION_SIGNATURE_FIELDS)
def test_signature_detects_any_row_change(tmp_path, monkeypatch, field, canonical):
    """No signature column may be skipped, including metadata on a middle row."""
    path = tmp_path / "state.db"
    fields = SESSION_SIGNATURE_FIELDS
    if canonical:
        from hermes_state import SessionDB

        with SessionDB(path) as db:
            for sid in ("a", "middle", "z"):
                db.create_session(sid, source="cli")
    else:
        with sqlite3.connect(path) as conn:
            conn.execute(f"CREATE TABLE sessions ({', '.join(fields)})")
            conn.executemany("INSERT INTO sessions(id) VALUES (?)", [("a",), ("middle",), ("z",)])
            conn.execute("CREATE TABLE messages (content TEXT)")
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    before = server._session_db_content_sig(path)
    with sqlite3.connect(path) as conn:
        if canonical:
            conn.execute("INSERT INTO gateway_heartbeats(backend_id, pid, started_at, last_heartbeat) "
                         "VALUES ('unrelated', 1, 1, 1)")
            conn.execute("INSERT INTO messages(session_id, role, content, timestamp) "
                         "VALUES ('middle', 'user', 'unrelated', 1)")
        else:
            conn.execute("INSERT INTO messages VALUES ('unrelated')")
    assert server._session_db_content_sig(path) == before
    with sqlite3.connect(path) as conn:
        conn.execute(f"UPDATE sessions SET {field} = ? WHERE id = 'middle'", ("changed",))
    changed = server._session_db_content_sig(path)
    assert changed != before
    assert changed[0] != "mtime-fallback"
    with sqlite3.connect(path) as conn:
        if canonical:
            conn.execute("INSERT INTO sessions(id, source, started_at) VALUES ('inserted', 'cli', 1)")
        else:
            conn.execute("INSERT INTO sessions(id) VALUES ('inserted')")
    inserted = server._session_db_content_sig(path)
    assert inserted != changed
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM sessions WHERE id = 'inserted'")
    assert server._session_db_content_sig(path) == changed


@pytest.mark.parametrize("journal", [False, True])
def test_signature_retries_busy_reads_without_false_changes(tmp_path, monkeypatch, journal):
    path = tmp_path / "state.db"
    _seed_signature_store(path, journal)
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        assert server._session_db_content_sig(path)[0] == "mtime-fallback"
        writer.rollback()
        before = server._session_db_content_sig(path)
        assert before[0] != "mtime-fallback"
        cached = server._sessions_db_sig_cache[str(path)]
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("UPDATE sessions SET title = 'after busy'")
        assert server._session_db_content_sig(path) == before
        assert server._sessions_db_sig_cache[str(path)] == cached
        writer.commit()
        assert server._session_db_content_sig(path) != before


def test_signature_bounds_dirty_tick_work(tmp_path, monkeypatch):
    """Like the repr guard above, count work rather than assert a machine-dependent time."""
    import hermes_state

    path = tmp_path / "state.db"
    with hermes_state.SessionDB(path) as db:
        for sid in ("a", "middle", "z"):
            db.create_session(sid, source="cli")
    connect = hermes_state._connect_tracked_db
    reads = []

    def traced_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)

        def authorize(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ and table == "sessions":
                reads.append(column)
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(hermes_state, "_connect_tracked_db", traced_connect)
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    before = server._session_db_content_sig(path)
    assert before[0] != "mtime-fallback"
    reads.clear()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO gateway_heartbeats(backend_id, pid, started_at, last_heartbeat) "
                     "VALUES ('unrelated', 1, 1, 1)")
    assert server._session_db_content_sig(path) == before
    assert reads == [], "unchanged projection scanned sessions on a dirty-WAL tick"
    import marshal

    dumps = marshal.dumps
    encoded = []

    def counted_dumps(row, version):
        encoded.append(row)
        return dumps(row, version)

    monkeypatch.setattr(marshal, "dumps", counted_dumps)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET title = 'changed' WHERE id = 'middle'")
    assert server._session_db_content_sig(path) != before
    assert len(encoded) == 1, "one changed row re-encoded the whole sessions table"


def test_projection_revision_retains_digest_semantics_and_falls_back(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("middle", source="cli")
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    original = server._session_db_content_sig(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET title = 'rolled back'")
        conn.rollback()
    assert server._session_db_content_sig(path) == original
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET title = 'temporary'")
        conn.execute("UPDATE sessions SET title = NULL")
    assert server._session_db_content_sig(path) == original
    # A temporarily missing trigger cannot let an uncovered write reuse a token.
    with sqlite3.connect(path) as conn:
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'sessions_projection_update'").fetchone()[0]
        conn.execute("DROP TRIGGER sessions_projection_update")
        conn.execute("UPDATE sessions SET title = 'uncovered'")
        conn.execute(trigger)
    changed = server._session_db_content_sig(path)
    assert changed != original
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER sessions_projection_update")
        conn.execute("UPDATE sessions SET title = 'still uncovered'")
    assert server._session_db_content_sig(path) != changed


def test_projection_revision_and_digest_share_a_snapshot(tmp_path, monkeypatch):
    import hermes_state_session_signature as projection
    from hermes_state import SessionDB

    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("middle", source="cli")
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    revision = projection.session_signature_revision

    def commit_after_revision(conn, db_path):
        token = revision(conn, db_path)
        with sqlite3.connect(path) as writer:
            writer.execute("UPDATE sessions SET title = 'racing commit'")
        return token

    with monkeypatch.context() as race:
        race.setattr(projection, "session_signature_revision", commit_after_revision)
        before = server._session_db_content_sig(path)
    assert server._session_db_content_sig(path) != before


def test_projection_journal_is_bounded_and_lagging_watcher_rebuilds(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_state_session_signature import SESSION_SIGNATURE_HISTORY

    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("middle", source="cli")
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    original = server._session_db_content_sig(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET title = 'older than retained history'")
        conn.executemany("INSERT INTO sessions(id, source, started_at) VALUES (?, 'cli', 1)",
                         [(f"new-{i}",) for i in range(SESSION_SIGNATURE_HISTORY + 1)])
        assert conn.execute("SELECT COUNT(*) FROM session_projection_changes").fetchone()[0] <= SESSION_SIGNATURE_HISTORY
    changed = server._session_db_content_sig(path)
    assert changed != original
    # A fresh full scan must agree with the lagging watcher's result.
    server._sessions_db_sig_cache.clear()
    assert server._session_db_content_sig(path) == changed


def test_projection_preserves_duplicate_rows_and_rowid_moves(tmp_path, monkeypatch):
    from hermes_state_session_signature import SESSION_SIGNATURE_TABLE_SQL, SESSION_SIGNATURE_TRIGGER_SQL

    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE sessions ({', '.join(SESSION_SIGNATURE_FIELDS)})")
        conn.executemany("INSERT INTO sessions(id) VALUES (?)", [(None,), (None,)])
        conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO state_meta VALUES ('db_file_generation', 'duplicate-rows')")
        conn.executescript(SESSION_SIGNATURE_TABLE_SQL + SESSION_SIGNATURE_TRIGGER_SQL)
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    original = server._session_db_content_sig(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET title = 'both duplicate rows'")
    changed = server._session_db_content_sig(path)
    assert changed != original  # XOR of per-row hashes would miss this.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET rowid = 30 WHERE rowid = 1")
    assert server._session_db_content_sig(path) == changed
    server._sessions_db_sig_cache.clear()
    assert server._session_db_content_sig(path) == changed
    with sqlite3.connect(path) as conn:
        conn.execute("VACUUM")
    assert server._session_db_content_sig(path) == changed
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET title = 'after vacuum' WHERE rowid = 1")
    after_vacuum = server._session_db_content_sig(path)
    assert after_vacuum != changed
    server._sessions_db_sig_cache.clear()
    assert server._session_db_content_sig(path) == after_vacuum


def test_canonical_projection_preserves_rowid_moves_and_vacuum(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_state_session_signature import SessionProjection

    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        for sid in ("a", "middle", "z"):
            db.create_session(sid, source="cli")
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    original = server._session_db_content_sig(path)
    projection = server._sessions_db_sig_cache[str(path)][2]
    assert isinstance(projection, SessionProjection)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET rowid = 99 WHERE id = 'middle'")
    assert server._session_db_content_sig(path) == original
    assert server._sessions_db_sig_cache[str(path)][2] is projection
    assert 99 in projection.hashes and 2 not in projection.hashes
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET title = 'moved row' WHERE rowid = 99")
    changed = server._session_db_content_sig(path)
    assert changed != original
    assert server._sessions_db_sig_cache[str(path)][2] is projection
    server._sessions_db_sig_cache.clear()
    assert server._session_db_content_sig(path) == changed
    with sqlite3.connect(path) as conn:
        conn.execute("VACUUM")
    # VACUUM changes schema_version. Keep the schema-change rebuild safeguard,
    # then prove the canonical modular sum resumes indexed incremental updates.
    assert server._session_db_content_sig(path) == changed
    projection = server._sessions_db_sig_cache[str(path)][2]
    assert isinstance(projection, SessionProjection)
    with sqlite3.connect(path) as conn:
        rowid = conn.execute("SELECT rowid FROM sessions WHERE id = 'middle'").fetchone()[0]
        conn.execute("UPDATE sessions SET title = 'after vacuum' WHERE rowid = ?", (rowid,))
    after_vacuum = server._session_db_content_sig(path)
    assert after_vacuum != changed
    assert server._sessions_db_sig_cache[str(path)][2] is projection
    server._sessions_db_sig_cache.clear()
    assert server._session_db_content_sig(path) == after_vacuum


def test_legacy_signature_detects_swapped_metadata_without_unique_ids(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions (title TEXT)")
        conn.executemany("INSERT INTO sessions VALUES (?)", [("first",), ("second",)])
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    before = server._session_db_content_sig(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET title = CASE title WHEN 'first' THEN 'second' ELSE 'first' END")
    assert server._session_db_content_sig(path) != before


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_projection_catches_replace_deletes_with_recursive_triggers_off(tmp_path, monkeypatch, operation):
    from hermes_state import SessionDB

    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("a", source="cli")
        db.create_session("middle", source="cli")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET title = 'unique title' WHERE id = 'a'")
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    before = server._session_db_content_sig(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA recursive_triggers=OFF")
        if operation == "insert":
            conn.execute("INSERT OR REPLACE INTO sessions(id, source, started_at, title) "
                         "VALUES ('inserted', 'cli', 1, 'unique title')")
        else:
            conn.execute("UPDATE OR REPLACE sessions SET title = 'unique title' WHERE id = 'middle'")
    changed = server._session_db_content_sig(path)
    assert changed != before
    server._sessions_db_sig_cache.clear()
    assert server._session_db_content_sig(path) == changed


def test_projection_detects_same_inode_restore_from_diverged_copy(tmp_path, monkeypatch):
    import shutil
    from hermes_state import SessionDB

    path, copy = tmp_path / "state.db", tmp_path / "copy.db"
    with SessionDB(path) as db:
        db.create_session("middle", source="cli")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    shutil.copyfile(path, copy)
    for target, title in ((path, "first branch"), (copy, "second branch")):
        conn = sqlite3.connect(target)
        conn.execute("UPDATE sessions SET title = ?", (title,))
        conn.commit()
        conn.close()
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    before = server._session_db_content_sig(path)
    inode = path.stat().st_ino
    shutil.copyfile(copy, path)
    assert path.stat().st_ino == inode
    restored = server._session_db_content_sig(path)
    assert restored != before
    server._sessions_db_sig_cache.clear()
    assert server._session_db_content_sig(path) == restored
