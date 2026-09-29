"""Real-SQLite contracts for generation-owned admission (default-off coordinator)."""
from __future__ import annotations

import concurrent.futures
import json
import sqlite3
from contextlib import closing

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity


def _pair(tmp_path):
    store = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="slot-a")
    new = GenerationIdentity.create(release_sha="b", label="slot-b")
    store.register(old, state="draining")
    store.register(new, state="serving")
    previous = store.acquire_lease("active_generation", old.id)
    assert store.release_lease("active_generation", old.id, previous)
    current = store.acquire_lease("active_generation", new.id)
    assert current == previous + 1
    return store, old, new, current


def _source(sender="sender"):
    return json.dumps({"version": 1, "sender": sender, "authorized": True}).encode()


def test_owned_rows_are_durable_ordered_and_duplicates_return_original(tmp_path):
    store, old, new, epoch = _pair(tmp_path)
    store.claim_session("home-a", "telegram", "chat", old.id, epoch - 1, outstanding_work=1)
    one, fresh = store.enqueue("home-a", "telegram", "chat", "update-10", "message",
                               _source(), b"first", new.id, epoch)
    two, second_fresh = store.enqueue("home-a", "telegram", "chat", "update-11", "stop",
                                      _source(), b"/stop", new.id, epoch)
    duplicate, duplicate_fresh = store.enqueue("home-a", "telegram", "chat", "update-10", "message",
                                               _source("attacker"), b"changed", new.id, epoch)
    assert fresh and second_fresh and not duplicate_fresh
    assert (one["owner_id"], one["seq"], one["state"]) == (old.id, 1, "pending")
    assert (two["owner_id"], two["seq"]) == (old.id, 2)
    assert duplicate == one
    assert [row["id"] for row in store.pending(old.id, "home-a", "telegram", "chat")] == [one["id"], two["id"]]
    with closing(store.connect()) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_idle_claim_and_atomic_transfer_preserve_sequence(tmp_path):
    store, old, new, epoch = _pair(tmp_path)
    store.claim_session("home-a", "telegram", "chat", old.id, epoch - 1, outstanding_work=1)
    first, _ = store.enqueue("home-a", "telegram", "chat", "one", "message", _source(), b"one", new.id, epoch)
    assert not store.transfer_session("home-a", "telegram", "chat", old.id, epoch - 1, new.id, epoch)
    store.set_outstanding("home-a", "telegram", "chat", old.id, epoch - 1, 0)
    assert store.transfer_session("home-a", "telegram", "chat", old.id, epoch - 1, new.id, epoch)
    assert store.pending(old.id, "home-a", "telegram", "chat") == []
    assert store.pending(new.id, "home-a", "telegram", "chat")[0]["id"] == first["id"]
    second, fresh = store.enqueue("home-a", "telegram", "chat", "two", "message", _source(), b"two", new.id, epoch)
    assert fresh and second["seq"] == 2
    idle, _ = store.enqueue("home-b", "telegram", "idle", "first", "message", _source(), b"next", new.id, epoch)
    assert idle["owner_id"] == new.id and idle["seq"] == 1


def test_fenced_acceptance_and_refusal_are_immutable(tmp_path):
    store, old, new, epoch = _pair(tmp_path)
    store.claim_session("home-a", "telegram", "chat", old.id, epoch - 1, outstanding_work=1)
    first, _ = store.enqueue("home-a", "telegram", "chat", "one", "message", _source(), b"one", new.id, epoch)
    second, _ = store.enqueue("home-a", "telegram", "chat", "two", "message", _source(), b"two", new.id, epoch)
    assert not store.disposition(second["id"], old.id, epoch - 1, "accepted")  # no leapfrogging
    assert not store.disposition(first["id"], new.id, epoch, "accepted")
    assert store.disposition(first["id"], old.id, epoch - 1, "refused")
    assert not store.disposition(first["id"], old.id, epoch - 1, "accepted")
    assert store.disposition(second["id"], old.id, epoch - 1, "accepted")
    assert not store.disposition(second["id"], old.id, epoch - 1, "accepted")


def test_concurrent_duplicate_and_session_sequence_are_serialized(tmp_path):
    store, old, new, epoch = _pair(tmp_path)
    def enqueue(event_id):
        return store.enqueue("home-a", "telegram", "chat", event_id, "message", _source(),
                             event_id.encode(), new.id, epoch)
    ids = ["same"] * 10 + [f"distinct-{n}" for n in range(10)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(enqueue, ids))
    assert len({row["id"] for row, _ in results}) == 11
    assert sum(fresh for _, fresh in results) == 11
    rows = store.pending(new.id, "home-a", "telegram", "chat")
    assert [row["seq"] for row in rows] == list(range(1, 12))


def test_old_coordinator_retains_generation_and_gains_tables(tmp_path):
    path = tmp_path / "gateway-coordinator.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('version','1');
            CREATE TABLE generations (
                id TEXT PRIMARY KEY, release_sha TEXT NOT NULL, label TEXT NOT NULL,
                pid INTEGER NOT NULL, started_at REAL NOT NULL, boot_id TEXT NOT NULL,
                start_fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                heartbeat_at REAL NOT NULL, drain_deadline REAL, suspect_from_state TEXT);
            CREATE TABLE leases (
                resource TEXT PRIMARY KEY, epoch INTEGER NOT NULL, generation_id TEXT NOT NULL,
                state TEXT NOT NULL, FOREIGN KEY(generation_id) REFERENCES generations(id));
            INSERT INTO generations VALUES ('old','old-release','slot-a',1,1,'boot','1:1','draining',1,NULL,NULL);
        """)
    store = GenerationCoordinator(tmp_path)
    assert store.generations()[0]["release_sha"] == "old-release"
    with closing(store.connect()) as db:
        assert db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == "1"
    next_generation = GenerationIdentity.create(release_sha="new", label="slot-b")
    store.register(next_generation, state="serving")
    epoch = store.acquire_lease("active_generation", next_generation.id)
    row, fresh = store.enqueue("home", "telegram", "chat", "update", "message", _source(),
                               b"hello", next_generation.id, epoch)
    assert fresh and row["seq"] == 1


def test_dead_owner_holds_pending_rows_without_replaying(tmp_path, monkeypatch):
    store, old, new, epoch = _pair(tmp_path)
    store.claim_session("home-a", "telegram", "chat", old.id, epoch - 1, outstanding_work=1)
    row, _ = store.enqueue("home-a", "telegram", "chat", "one", "message",
                           _source(), b"work", new.id, epoch)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    assert store.hold_dead_owner(old.id) == 1
    assert store.pending(old.id, "home-a", "telegram", "chat") == []
    with closing(store.connect()) as conn:
        held = conn.execute("SELECT state,owner_id FROM inbox WHERE id=?", (row["id"],)).fetchone()
        assert tuple(held) == ("interrupted", old.id)
    again, fresh = store.enqueue("home-a", "telegram", "chat", "one", "message",
                                 _source(), b"work", new.id, epoch)
    assert not fresh and again["state"] == "interrupted"


def test_invalid_source_and_oversized_payload_fail_closed(tmp_path):
    store, old, new, epoch = _pair(tmp_path)
    with pytest.raises(ValueError):
        store.enqueue("home", "telegram", "chat", "one", "message", b"{}", b"data", new.id, epoch)
    with pytest.raises(ValueError):
        store.enqueue("home", "telegram", "chat", "two", "message", _source(), b"x" * (1024 * 1024 + 1), new.id, epoch)
    assert store.pending(new.id, "home", "telegram", "chat") == []
