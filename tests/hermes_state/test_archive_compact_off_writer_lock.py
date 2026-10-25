"""archive_and_compact must not hold the writer lock while it re-reads the session's history.

The compaction commit re-folds display generations (#122167) by recomputing the display key of
every display-visible row: every row the session ever compacted. On 2026-10-11 10:57 one such
commit held ``SessionDB._lock`` while 21 turns and 55 threads queued behind it. The history
read now happens on a pooled reader before the lock; under the lock only rows the snapshot did
not see, or that changed since, are recomputed. The fold result must stay identical.
"""

import json
import threading

import pytest

from agent.context_compressor import (
    HISTORICAL_TASK_HEADING,
    SUMMARY_PREFIX,
    _MERGED_PRIOR_CONTEXT_HEADER,
    _MERGED_SUMMARY_DELIMITER,
    _SUMMARY_END_MARKER,
    split_user_originated_turn,
)
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


@pytest.mark.parametrize(("column", "value"), [
    ("content", "question rewritten"), ("role", "assistant"), ("display_kind", "hidden"),
    ("timestamp", 123.0), ("tool_call_id", "changed-call"),
    ("tool_calls", '[{"id": "changed-call", "type": "function"}]'), ("tool_name", "changed-tool"),
])
def test_rows_changed_after_the_snapshot_are_recomputed_under_the_lock(db, column, value, monkeypatch, caplog):
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
    db._execute_write(lambda conn: conn.execute(
        f"UPDATE messages SET {column} = ? WHERE id = ?", (value, rewritten)))
    assert db._read_one("SELECT display_identity FROM messages WHERE id = ?", (rewritten,))[0] is None
    unseen = db.append_message(sid, "user", "arrived after the snapshot", timestamp=999.0)
    assert unseen not in plan
    monkeypatch.setattr(db, "_WRITE_LOCK_WARN_S", 0.0)
    caplog.clear()

    db._execute_write(lambda conn: db._reconcile_display_orders(conn, sid, plan))
    planned = _fold_state(db, sid)
    warning = next(record.message for record in caplog.records if "held the write lock" in record.message)
    assert "display_updates=" in warning
    db._execute_write(lambda conn: db._reconcile_display_orders(conn, sid))
    assert _fold_state(db, sid) == planned
    assert all(identity is not None and identity != bytes(32) for _id, _order, identity in planned)


@pytest.mark.parametrize("shape", ["plain", "summary", "composite", "multimodal", "hidden"])
@pytest.mark.parametrize("metadata", [
    {"reactions": [{"emoji": "heart", "count": 2}], "delivery_notice": "changed"},
    {"model_only": True},
    {"model_only": False, "task_count": 3},
])
def test_metadata_edits_preserve_keys_without_historical_payload_reads(db, shape, metadata):
    """Metadata affects presentation/visibility, not the content-only handoff identity."""
    sid = "topic"
    _session_with_history(db, sid)
    summary = f"{SUMMARY_PREFIX}\n\n{HISTORICAL_TASK_HEADING}\nold work\n\n{_SUMMARY_END_MARKER}"
    content = {
        "plain": "live ask",
        "hidden": "hidden live ask",
        "summary": summary,
        "composite": f"{_MERGED_PRIOR_CONTEXT_HEADER}\nlive ask\n\n{_MERGED_SUMMARY_DELIMITER}\n\n{summary}",
        "multimodal": [{"type": "text", "text": "live ask"},
                       {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}],
    }[shape]
    row_id = db.append_message(sid, "user", content, timestamp=100.0,
                               display_kind="hidden" if shape == "hidden" else None)
    # Exercise an archived row, including visibility in both directions after the plan.
    db._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET active = 0, compacted = 1, display_metadata = ? WHERE id = ?",
        (json.dumps({"model_only": not metadata.get("model_only", False)}), row_id)))
    plan = db._display_fold_plan(sid)
    before = db._read_one("SELECT * FROM messages WHERE id = ?", (row_id,))
    db._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET display_metadata = ? WHERE id = ?", (json.dumps(metadata), row_id)))
    after = db._read_one("SELECT * FROM messages WHERE id = ?", (row_id,))
    key = db._display_dedupe_key(before)
    assert key[0] == "user"
    handoff, live = split_user_originated_turn({"role": "user", "content": content})
    if shape == "composite":
        assert handoff is not None and live is not None
        assert live["content"] == "live ask"
        assert key[1] == db._encode_content("live ask")
    elif shape == "summary":
        assert handoff is not None and live is None
    assert key == db._display_dedupe_key(after)
    assert before["display_identity"] == after["display_identity"]

    statements = []

    def fold(conn):
        conn.set_trace_callback(statements.append)
        try:
            db._reconcile_display_orders(conn, sid, plan)
        finally:
            conn.set_trace_callback(None)

    db._execute_write(fold)
    planned = _fold_state(db, sid)
    projection = db.get_messages(sid, include_compacted=True)
    db._execute_write(lambda conn: db._reconcile_display_orders(conn, sid))
    assert _fold_state(db, sid) == planned
    assert db.get_messages(sid, include_compacted=True) == projection
    presented = [message for message in projection if message["id"] == row_id]
    assert bool(presented) == (not metadata.get("model_only", False))
    if presented:
        assert presented[0]["display_metadata"] == metadata
    payload_reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")
                     and "FROM MESSAGES" in sql.upper()
                     and any(column in sql.upper().split("FROM", 1)[0]
                             for column in ("DISPLAY_METADATA", "CONTENT", "*"))]
    assert not payload_reads, "unchanged historical payloads were reread under the writer lock"


@pytest.mark.parametrize("wal_plan", [False, True])
def test_fold_failure_rolls_back_archive_rows_and_display_state(db, monkeypatch, wal_plan):
    sid = "topic"
    _session_with_history(db, sid)
    before = [tuple(row) for row in db._read_all("SELECT * FROM messages ORDER BY id")]
    session = tuple(db._read_one("SELECT * FROM sessions WHERE id = ?", (sid,)))
    real_reconcile = db._reconcile_display_orders
    if not wal_plan:
        monkeypatch.setattr(db, "_display_fold_plan", lambda _sid: None)

    def fail_after_fold(conn, session_id, plan=None):
        real_reconcile(conn, session_id, plan)
        raise RuntimeError("fold failed")

    monkeypatch.setattr(db, "_reconcile_display_orders", fail_after_fold)
    live = db.get_messages_as_conversation(sid)
    with pytest.raises(RuntimeError, match="fold failed"):
        db.archive_and_compact(sid, [{"role": "user", "content": "replacement summary"}, *live[-2:]],
                               tail_count=2)
    assert [tuple(row) for row in db._read_all("SELECT * FROM messages ORDER BY id")] == before
    assert tuple(db._read_one("SELECT * FROM sessions WHERE id = ?", (sid,))) == session


def test_no_plan_without_a_wal_reader(db, monkeypatch, caplog):
    _session_with_history(db, "topic")
    before = _fold_state(db, "topic")
    monkeypatch.setattr(db, "_wal_active", False)
    assert db._display_fold_plan("topic") is None
    monkeypatch.setattr(db, "_WRITE_LOCK_WARN_S", 0.0)
    caplog.clear()
    db._execute_write(lambda conn: db._reconcile_display_orders(conn, "topic"))
    assert _fold_state(db, "topic") == before
    warning = next(record.message for record in caplog.records if "held the write lock" in record.message)
    assert "display_full_fold=" in warning
    assert "display_planned_fold=" not in warning
