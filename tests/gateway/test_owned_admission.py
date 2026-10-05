"""Real-SQLite contracts for generation-owned admission (default-off coordinator)."""
from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
from contextlib import closing

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity
from gateway.status import _get_process_start_time


def _pair(tmp_path, *, old_pid=None):
    store = GenerationCoordinator(tmp_path)
    fingerprint = f"{os.getpid()}:{_get_process_start_time(os.getpid())}"
    old_pid = os.getpid() if old_pid is None else old_pid
    old = GenerationIdentity.create(release_sha="a", label="slot-a", pid=old_pid,
                                    start_fingerprint=f"{old_pid}:{_get_process_start_time(old_pid)}")
    new = GenerationIdentity.create(release_sha="b", label="slot-b", start_fingerprint=fingerprint)
    store.register(old, state="serving")
    store.register(new, state="standby")
    previous = store.acquire_lease("active_generation", old.id)
    store.request_transfer(old.id, new.id, previous, set())
    current = store.commit_transfer(old.id, new.id, previous)
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
        retired = conn.execute("SELECT state,verdict,verdict_at,verdict_evidence FROM generations WHERE id=?",
                               (old.id,)).fetchone()
        assert tuple(retired)[:2] == ("exited", "failed")
        assert retired["verdict_at"] is not None and retired["verdict_evidence"]
    again, fresh = store.enqueue("home-a", "telegram", "chat", "one", "message",
                                 _source(), b"work", new.id, epoch)
    assert not fresh and again["state"] == "interrupted"
    later, fresh = store.enqueue("home-a", "telegram", "chat", "two", "message",
                                 _source(), b"next", new.id, epoch)
    assert fresh and later["owner_id"] == new.id and later["seq"] == 2






@pytest.mark.parametrize("dead", [False, True])
def test_dead_owner_is_probed_once_against_locked_ownership(tmp_path, monkeypatch, dead):
    store, old, new, epoch = _pair(tmp_path)
    store.claim_session("home-a", "telegram", "chat", old.id, epoch - 1, outstanding_work=1)
    probes = []
    def probe(generation):
        probes.append(generation["pid"])
        # Ownership cannot change between the death proof and releasing claims.
        with closing(store.connect()) as other:
            other.execute("PRAGMA busy_timeout=1")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        return dead
    monkeypatch.setattr(store, "_owner_is_dead", probe)
    row, fresh = store.enqueue("home-a", "telegram", "chat", "next", "message",
                               _source(), b"next", new.id, epoch)
    assert fresh and len(probes) == 1
    assert row["owner_id"] == (new.id if dead else old.id)
    with closing(store.connect()) as db:
        retired = db.execute("SELECT state,verdict FROM generations WHERE id=?", (old.id,)).fetchone()
        assert tuple(retired) == (("exited", "failed") if dead else ("draining", None))


def test_first_message_after_owner_death_moves_to_successor(tmp_path, monkeypatch):
    store, old, new, epoch = _pair(tmp_path)
    store.claim_session("home-a", "telegram", "chat", old.id, epoch - 1, outstanding_work=1)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    row, fresh = store.enqueue("home-a", "telegram", "chat", "next", "message",
                               _source(), b"next", new.id, epoch)
    assert fresh and row["owner_id"] == new.id and row["state"] == "pending"


def test_exited_owner_outstanding_claim_moves_without_replaying_cut_work(tmp_path):
    store, old, new, epoch = _pair(tmp_path)
    store.claim_session("home-a", "telegram", "chat", old.id, epoch - 1, outstanding_work=1)
    row, _ = store.enqueue("home-a", "telegram", "chat", "one", "message",
                           _source(), b"cut-work", new.id, epoch)
    store.heartbeat(old.id, state="exited")
    assert store.release_exited_owner(old.id) == 1
    later, fresh = store.enqueue("home-a", "telegram", "chat", "two", "message",
                                 _source(), b"next", new.id, epoch)
    assert fresh and later["owner_id"] == new.id
    with closing(store.connect()) as db:
        assert db.execute("SELECT state FROM inbox WHERE id=?", (row["id"],)).fetchone()[0] == "interrupted"




@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("failure", ["cap", "sigkill"])
def test_cut_claim_recovers_only_on_next_new_message(tmp_path, failure):
    import signal
    import subprocess
    import sys
    import time

    process = None
    try:
        if failure == "sigkill":
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        store, old, new, epoch = _pair(tmp_path, old_pid=process.pid if process else None)
        store.claim_session("home-a", "telegram", "chat", old.id, epoch - 1, outstanding_work=1)
        cut, _ = store.enqueue("home-a", "telegram", "chat", "cut", "message",
                               _source(), b"cut-work", new.id, epoch)
        if failure == "cap":
            with closing(store.connect()) as db, db:
                db.execute("UPDATE generations SET state='draining',drain_deadline=? WHERE id=?",
                           (time.time() - 1, old.id))
            assert store.fence_draining_generation(old.id) == 1
            store.heartbeat(old.id, state="exited")
            assert store.release_exited_owner(old.id) == 0
        else:
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            assert store.hold_dead_owner(old.id) == 1
        with closing(store.connect()) as db:
            claim = db.execute("SELECT generation_id,state,outstanding_work FROM sessions WHERE session_key='chat'").fetchone()
            assert tuple(claim) == (new.id, "interrupted", 0)
        duplicate, fresh = store.enqueue("home-a", "telegram", "chat", "cut", "message",
                                         _source(), b"cut-work", new.id, epoch)
        assert not fresh and duplicate["id"] == cut["id"] and duplicate["state"] == "interrupted"
        row, fresh = store.enqueue("home-a", "telegram", "chat", "next", "message",
                                   _source(), b"new-work", new.id, epoch)
        assert fresh and row["owner_id"] == new.id and row["state"] == "pending"
        assert [item["id"] for item in store.pending(new.id, "home-a", "telegram", "chat")] == [row["id"]]
        assert store.disposition(row["id"], new.id, epoch, "accepted")
        again, fresh = store.enqueue("home-a", "telegram", "chat", "next", "message",
                                     _source(), b"new-work", new.id, epoch)
        assert not fresh and again["state"] == "accepted"
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_interrupted_row_does_not_replay_after_handler_failure(tmp_path):
    store, old, new, epoch = _pair(tmp_path)
    row, _ = store.enqueue("home-a", "telegram", "chat", "one", "message",
                           _source(), b"work", new.id, epoch)
    assert store.interrupt_row(row["id"], new.id, epoch)
    duplicate, fresh = store.enqueue("home-a", "telegram", "chat", "one", "message",
                                     _source(), b"work", new.id, epoch)
    assert not fresh and duplicate["state"] == "interrupted"


def test_invalid_source_and_oversized_payload_fail_closed(tmp_path):
    store, old, new, epoch = _pair(tmp_path)
    with pytest.raises(ValueError):
        store.enqueue("home", "telegram", "chat", "one", "message", b"{}", b"data", new.id, epoch)
    with pytest.raises(ValueError):
        store.enqueue("home", "telegram", "chat", "two", "message", _source(), b"x" * (1024 * 1024 + 1), new.id, epoch)
    assert store.pending(new.id, "home", "telegram", "chat") == []
