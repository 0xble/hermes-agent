from __future__ import annotations

import builtins
import itertools
import math
import sqlite3

import pytest

from tui_gateway import server


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


@pytest.mark.parametrize("before_value, after_value", [
    (1800000000.1234567, math.nextafter(1800000000.1234567, math.inf)),
    (None, "None"),
    ("a\0b", "a\0c"),
    ("é\n'\\", "é\r'\\"),
    (b"text", "text"),
])
def test_session_signature_keeps_exact_sqlite_values(tmp_path, monkeypatch, before_value, after_value):
    db_path = tmp_path / "state.db"
    _seed_store(db_path)
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ticks = itertools.count()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda paths: next(ticks))
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE sessions SET title = ?", (before_value,))
    conn.commit()
    before = server._session_db_content_sig(db_path)
    assert before == server._session_db_content_sig(db_path)
    conn.execute("UPDATE sessions SET title = ?", (after_value,))
    conn.commit()
    after = server._session_db_content_sig(db_path)
    conn.close()
    assert before != after
    assert before[0] != "mtime-fallback"
    assert after[0] != "mtime-fallback"
