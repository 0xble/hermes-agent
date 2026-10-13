"""archive_and_compact must not hold the writer lock while it re-reads the session's history.

The compaction commit re-folds display generations (#122167) by recomputing the display key of
every display-visible row: every row the session ever compacted. On 2026-10-11 10:57 one such
commit held ``SessionDB._lock`` while 21 turns and 55 threads queued behind it. The history
read now happens on a pooled reader before the lock; under the lock only rows the snapshot did
not see, or that changed since, are recomputed. The fold result must stay identical.
"""

import threading

import pytest

from hermes_state import SessionDB


def _fold_state(db, sid):
    return [tuple(row) for row in db._read_all(
        "SELECT id, display_order, display_identity FROM messages "
        "WHERE session_id = ? AND (active = 1 OR compacted = 1) ORDER BY id", (sid,))]


def _session_with_history(db, sid, turns=30):
    db.create_session(sid, source="telegram")
    for i in range(turns):
        db.append_message(sid, "user", f"question {i}", timestamp=1727000000.0 + 2 * i)
        db.append_message(sid, "assistant", f"answer {i}", timestamp=1727000001.0 + 2 * i)
    live = db.get_messages_as_conversation(sid)
    db.archive_and_compact(sid, [{"role": "user", "content": "[summary]"}] + live[-2:], tail_count=2)
    for i in range(turns, turns + 3):
        db.append_message(sid, "user", f"question {i}", timestamp=1727000000.0 + 2 * i)
        db.append_message(sid, "assistant", f"answer {i}", timestamp=1727000001.0 + 2 * i)
    return db._read_one("SELECT MAX(id) FROM messages WHERE session_id = ?", (sid,))[0]


@pytest.fixture()
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    yield store
    store.close()


def test_writers_proceed_while_compaction_rereads_history(db, monkeypatch):
    sid = "topic"
    history_max = _session_with_history(db, sid)
    assert db._wal_active
    entered, release = threading.Event(), threading.Event()
    real_key = SessionDB._display_dedupe_key

    def slow_key(self, row):
        # The first historical row the compactor decodes stalls, as a cold page read does on a
        # saturated disk. Wherever that read runs, it must not be under the writer lock.
        row_id = row["id"] if "id" in row.keys() else None  # inserts key a plain dict
        if (threading.current_thread().name == "compactor" and row_id is not None
                and row_id <= history_max and not entered.is_set()):
            entered.set()
            assert release.wait(10), "history read never released"
        return real_key(self, row)

    monkeypatch.setattr(SessionDB, "_display_dedupe_key", slow_key)
    live = db.get_messages_as_conversation(sid)
    errors = []

    def compact():
        try:
            db.archive_and_compact(sid, [{"role": "user", "content": "[summary 2]"}] + live[-2:], tail_count=2)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    compactor = threading.Thread(target=compact, name="compactor")
    compactor.start()
    try:
        assert entered.wait(10), "compaction never read its history"
        # The compactor is event-gated inside historical decoding, so lock availability is
        # deterministic rather than inferred from whether a writer beats a timing deadline.
        assert db._lock.acquire(blocking=False), "history decoding holds the compaction writer lock"
        db._lock.release()
        db.set_meta("probe", "1")
    finally:
        release.set()
        compactor.join(10)
    assert not errors
    assert db.get_meta("probe") == "1"
    before = _fold_state(db, sid)
    db._execute_write(lambda conn: db._reconcile_display_orders(conn, sid))
    assert _fold_state(db, sid) == before, "the planned fold differs from a full re-fold"


def test_rows_changed_after_the_snapshot_are_recomputed_under_the_lock(db):
    sid = "topic"
    _session_with_history(db, sid)
    plan = db._display_fold_plan(sid)
    assert plan
    # After the snapshot: a historical row's identity drifts, and a live user row is rewritten
    # (the identity trigger nulls its stored identity). Neither may reuse the planned value.
    drifted = db._read_one(
        "SELECT MIN(id) FROM messages WHERE session_id = ? AND active = 0 AND compacted = 1", (sid,))[0]
    rewritten = db._read_one(
        "SELECT MAX(id) FROM messages WHERE session_id = ? AND active = 1 AND role = 'user'", (sid,))[0]
    db._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET display_order = id, display_identity = zeroblob(32) WHERE id = ?", (drifted,)))
    assert db.set_user_message_content(sid, rewritten, "question rewritten") == 1

    db._execute_write(lambda conn: db._reconcile_display_orders(conn, sid, plan))
    planned = _fold_state(db, sid)
    db._execute_write(lambda conn: db._reconcile_display_orders(conn, sid))
    assert _fold_state(db, sid) == planned
    assert all(identity is not None and identity != bytes(32) for _id, _order, identity in planned)


def test_user_metadata_edits_after_the_snapshot_are_recomputed(db, monkeypatch):
    """``display_metadata`` feeds a user row's display key but does not null the stored identity,
    so a metadata edit after the snapshot must still send that row back through the key."""
    sid = "topic"
    _session_with_history(db, sid)
    plan = db._display_fold_plan(sid)
    users = [row[0] for row in db._read_all(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'user' AND (active = 1 OR compacted = 1) "
        "ORDER BY id", (sid,))]
    edited, untouched = users[0], users[1]
    db._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET display_metadata = ? WHERE id = ?", ('{"reactions": []}', edited)))
    recomputed = []
    real_key = SessionDB._display_dedupe_key

    def spy(self, row):
        recomputed.append(row["id"] if "id" in row.keys() else None)
        return real_key(self, row)

    monkeypatch.setattr(SessionDB, "_display_dedupe_key", spy)
    db._execute_write(lambda conn: db._reconcile_display_orders(conn, sid, plan))
    assert edited in recomputed
    assert untouched not in recomputed
    monkeypatch.setattr(SessionDB, "_display_dedupe_key", real_key)
    planned = _fold_state(db, sid)
    db._execute_write(lambda conn: db._reconcile_display_orders(conn, sid))
    assert _fold_state(db, sid) == planned


def test_no_plan_without_a_wal_reader(db, monkeypatch):
    monkeypatch.setattr(db, "_wal_active", False)
    assert db._display_fold_plan("topic") is None
