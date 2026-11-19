"""A structural routing save writes only its diff inside the state.db write lock.

A full replace parsed, retirement-checked and re-selected every route (37,670 on the live store)
inside one write transaction, holding the writer lock 9-113 s and stalling every session's turn.
These tests pin the delta path's SQL footprint and its full-replace semantics, including the
first save, the periodic reconcile and a private interval, which read the stored rows off the writer.
"""
from __future__ import annotations

import copy
import json

import hermes_state
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from hermes_state_mutation_retirement import RETIRED_PREFIX

FULL_REPLACE_SQL = "NOT IN (SELECT key FROM json_each"
FULL_READ_SQL = "SELECT session_key, entry_json FROM gateway_routing WHERE scope"


def _source(user_id: str) -> SessionSource:
    return SessionSource(platform=Platform.LOCAL, chat_id=f"chat-{user_id}", chat_name="CLI",
                         chat_type="dm", user_id=user_id)


def _make_store(tmp_path, monkeypatch, count: int = 6) -> tuple[SessionStore, list]:
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: str(tmp_path))
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entries = [store.get_or_create_session(_source(f"user-{i}")) for i in range(count)]
    return store, entries


def _rows(store) -> dict[str, str]:
    return store._db.load_gateway_routing_entries(scope=store._routing_scope())


def _traced_save(store) -> tuple[list[str], int]:
    """Run one full routing save; return its SQL (parameters expanded) and rows it changed."""
    statements: list[str] = []
    conn = store._db._conn
    with store._db._lock:
        before = conn.total_changes
        conn.set_trace_callback(statements.append)
    try:
        store._save_entries()
    finally:
        with store._db._lock:
            conn.set_trace_callback(None)
            changed = conn.total_changes - before
    return [s for s in statements if "gateway_routing" in s], changed


def _routing_writes(statements: list[str]) -> list[str]:
    return [s for s in statements if s.lstrip().upper().startswith(("WITH", "INSERT", "DELETE", "UPDATE"))
            and "SELECT KEY FROM WANTED WHERE NOT EXISTS" not in s.upper()]


def test_delta_save_touches_only_changed_and_removed_keys(tmp_path, monkeypatch):
    store, entries = _make_store(tmp_path, monkeypatch)
    try:
        changed, removed = entries[1].session_key, entries[2].session_key
        untouched = [e.session_key for e in entries if e.session_key not in (changed, removed)]
        with store._lock:
            store._entries[changed].last_prompt_tokens = 4242
            del store._entries[removed]
        statements, row_changes = _traced_save(store)

        assert not any(FULL_REPLACE_SQL in s for s in statements), statements
        writes = _routing_writes(statements)
        assert writes, "the delta must still write the change"
        assert all(key not in s for s in statements for key in untouched), \
            "an unchanged route must not be sent to SQLite"
        assert row_changes == 2  # update one, delete one
        rows = _rows(store)
        assert json.loads(rows[changed])["last_prompt_tokens"] == 4242
        assert removed not in rows
        assert set(untouched) <= set(rows)

        # Nothing changed: no routing write at all.
        statements, row_changes = _traced_save(store)
        assert _routing_writes(statements) == [] and row_changes == 0
    finally:
        store._db.close()


def test_delta_save_skips_and_reports_a_retired_route(tmp_path, monkeypatch):
    store, entries = _make_store(tmp_path, monkeypatch, count=2)
    try:
        store._save_entries()
        ghost_key = "agent:main:local:dm:ghost"
        ghost = copy.copy(entries[0])
        ghost.session_id = "retired-session"
        store._db._write_sql("INSERT INTO state_meta (key, value) VALUES (?, ?)",
                             (RETIRED_PREFIX + "retired-session", "{}"))
        with store._lock:
            store._entries[ghost_key] = ghost
        statements, _ = _traced_save(store)

        assert not any(FULL_REPLACE_SQL in s for s in statements)
        assert ghost_key not in _rows(store)
        mirror = json.loads((tmp_path / "sessions" / "sessions.json").read_text())
        assert ghost_key not in mirror, "a skipped retired route stays out of the mirror"
        assert entries[0].session_key in mirror
        # Still reported (and so retried) on the next save, as a full replace does.
        statements, _ = _traced_save(store)
        assert any(ghost_key in s for s in _routing_writes(statements))
        assert ghost_key not in _rows(store)
    finally:
        store._db.close()


def _full_table(statements: list[str]) -> list[str]:
    """Statements that touch every routing row on the writer connection (a full replace or read)."""
    return [s for s in statements
            if FULL_REPLACE_SQL in s or (FULL_READ_SQL in s and "session_key IN" not in s)]


def test_first_save_after_start_restores_a_missing_row_without_a_full_replace(tmp_path, monkeypatch):
    store, entries = _make_store(tmp_path, monkeypatch, count=3)
    store._db.close()
    restarted = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    try:
        restarted.get_or_create_session(_source("user-0"))  # loads the index
        lost, kept = entries[1].session_key, entries[2].session_key
        restarted._db._write_sql("DELETE FROM gateway_routing WHERE session_key = ?", (lost,))
        assert restarted._routing_baseline is None, "no full save yet in this process"
        statements, row_changes = _traced_save(restarted)
        assert _full_table(statements) == [], "the reconcile reads stored rows off the writer"
        assert all(kept not in s for s in statements), "an unchanged route must not be sent to SQLite"
        assert row_changes == 1
        assert lost in _rows(restarted)
    finally:
        restarted._db.close()


def test_periodic_reconcile_restores_a_row_deleted_behind_the_process(tmp_path, monkeypatch):
    store, entries = _make_store(tmp_path, monkeypatch, count=3)
    try:
        store._save_entries()
        lost = entries[0].session_key
        store._db._write_sql("DELETE FROM gateway_routing WHERE session_key = ?", (lost,))

        statements, _ = _traced_save(store)
        assert _full_table(statements) == []
        assert lost not in _rows(store), "a delta trusts its baseline until the next reconcile"

        store._routing_full_at -= 601
        statements, row_changes = _traced_save(store)
        assert _full_table(statements) == []
        assert row_changes == 1
        assert lost in _rows(store)
    finally:
        store._db.close()


def test_failed_write_forces_the_next_save_to_reread_the_stored_rows(tmp_path, monkeypatch):
    store, entries = _make_store(tmp_path, monkeypatch, count=2)
    try:
        store._save_entries()

        def _fail(*args, **kwargs):
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr(store._db, "apply_gateway_routing_delta", _fail)
        with store._lock:
            store._entries[entries[0].session_key].last_prompt_tokens = 7
        store._save_entries()
        assert store._routing_baseline is None
        del store._db.apply_gateway_routing_delta
        statements, _ = _traced_save(store)
        assert _full_table(statements) == []
        assert json.loads(_rows(store)[entries[0].session_key])["last_prompt_tokens"] == 7
        assert store._routing_baseline is not None
    finally:
        store._db.close()


def test_private_interval_writes_the_public_diff_and_keeps_private_rows_byte_exact(tmp_path, monkeypatch):
    from agent.incognito import register_session

    store, entries = _make_store(tmp_path, monkeypatch, count=4)
    private_sid = entries[0].session_id
    try:
        store._save_entries()
        private_key, changed, removed, quiet = (e.session_key for e in entries)
        stored_private = _rows(store)[private_key]
        register_session(private_sid)
        with store._lock:
            store._entries[private_key].last_prompt_tokens = 31337
            store._entries[changed].last_prompt_tokens = 4242
            del store._entries[removed]
        store._routing_baseline = None  # a first save, or a reconcile, during the private interval
        statements, row_changes = _traced_save(store)

        assert _full_table(statements) == [], statements
        assert all(quiet not in s for s in statements)
        assert all(private_key not in s for s in _routing_writes(statements)), "a private route is never written"
        assert row_changes == 2
        rows = _rows(store)
        assert rows[private_key] == stored_private
        assert json.loads(rows[changed])["last_prompt_tokens"] == 4242
        assert removed not in rows and quiet in rows

        # A private route dropped from the index keeps its stored row until the interval ends.
        with store._lock:
            del store._entries[private_key]
        statements, _ = _traced_save(store)
        assert _full_table(statements) == []
        assert _rows(store)[private_key] == stored_private
        register_session(private_sid, False)
        _traced_save(store)
        assert private_key not in _rows(store)
    finally:
        register_session(private_sid, False)
        store._db.close()


def test_delayed_full_save_does_not_regress_a_newer_fast_save(tmp_path, monkeypatch):
    store, entries = _make_store(tmp_path, monkeypatch, count=3)
    try:
        store._save_entries()
        key = entries[0].session_key
        with store._lock:
            store._entries[key].last_prompt_tokens = 1
            stale_data, stale_generation = store._snapshot_routing_locked()
            store._entries[key].last_prompt_tokens = 2
        store._save_entry(key)  # newer revision lands first
        assert json.loads(_rows(store)[key])["last_prompt_tokens"] == 2

        store._persist_routing_data(stale_data, stale_generation)
        assert json.loads(_rows(store)[key])["last_prompt_tokens"] == 2

        statements, _ = _traced_save(store)
        assert _full_table(statements) == []
        assert json.loads(_rows(store)[key])["last_prompt_tokens"] == 2
        # A full save older than one already persisted writes nothing.
        with store._lock:
            older = store._snapshot_routing_locked()
            store._entries[key].last_prompt_tokens = 3
        store._save_entries()
        store._persist_routing_data(*older)
        assert json.loads(_rows(store)[key])["last_prompt_tokens"] == 3
    finally:
        store._db.close()
