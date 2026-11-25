"""Active replay stays bounded by the live context, not archived generations.

Exercise SessionDB's public replay API on real SQLite stores. VM-step ratios
avoid flaky wall-clock budgets while complete projection comparisons guard
prompt bytes, id order, tool pairing, display reads, and repaired history.
"""

import copy

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    handle = SessionDB(tmp_path / "state.db")
    try:
        yield handle
    finally:
        handle.close()


def _archive(db, sid, count):
    # Disposable historical generations; active rows use the real append API.
    db._execute_write(lambda conn: conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp, active, compacted) "
        "VALUES (?, 'assistant', ?, ?, 0, 1)",
        [(sid, f"archived-{i}", float(count - i)) for i in range(count)],
    ))


def _live(db, sid, label):
    messages = [
        {"role": "user", "content": f"{label} ask", "timestamp": 300.0},
        {"role": "assistant", "content": "", "timestamp": 200.0,
         "tool_calls": [{"id": f"{label}-call", "type": "function",
                         "function": {"name": "terminal", "arguments": '{"command":"pwd"}'}}],
         "reasoning_content": "  unchanged reasoning  ",
         "reasoning_details": [{"type": "reasoning.text", "text": "detail"}],
         "codex_reasoning_items": [{"type": "reasoning", "id": "r"}],
         "codex_message_items": [{"type": "message", "id": "m"}]},
        {"role": "tool", "content": f"{label} result", "timestamp": 100.0,
         "tool_call_id": f"{label}-call", "tool_name": "terminal"},
        {"role": "assistant", "content": f"{label} answer", "timestamp": 100.0,
         "api_content": "  exact provider answer\n", "finish_reason": "stop"},
    ]
    db.append_messages_batch(sid, messages)
    return messages


def _measured_replay(reader, sid, **kwargs):
    callbacks = 0

    def progress():
        nonlocal callbacks
        callbacks += 1
        return 0

    reader._conn.set_progress_handler(progress, 100)
    try:
        result = reader.get_messages_as_conversation(sid, **kwargs)
    finally:
        reader._conn.set_progress_handler(None, 0)
    return result, callbacks


@pytest.mark.parametrize("include_ancestors", [False, True])
def test_active_replay_work_does_not_scale_with_archive(db, include_ancestors):
    for label, count in (("small", 2_000), ("large", 20_000)):
        root, tip = f"{label}-root", f"{label}-tip"
        db.create_session(root, source="cli")
        _archive(db, root, count)
        _live(db, root, f"{label} root")
        db.end_session(root, end_reason="compression")
        db.create_session(tip, source="cli", parent_session_id=root)
        _archive(db, tip, count)
        _live(db, tip, f"{label} tip")

    reader = SessionDB(db.db_path, read_only=True)
    try:
        small, small_steps = _measured_replay(reader, "small-tip", include_ancestors=include_ancestors)
        large, large_steps = _measured_replay(reader, "large-tip", include_ancestors=include_ancestors)
        expected_count = 8 if include_ancestors else 4
        assert len(small) == len(large) == expected_count
        assert small_steps > 0
        assert large_steps < small_steps * 3, (small_steps, large_steps)
        for conversation in (small, large):
            for offset in range(0, expected_count, 4):
                turn = conversation[offset:offset + 4]
                assert [m["role"] for m in turn] == ["user", "assistant", "tool", "assistant"]
                assert [m["timestamp"] for m in turn] == [300.0, 200.0, 100.0, 100.0]
                assert turn[1]["tool_calls"][0]["id"] == turn[2]["tool_call_id"]
                assert turn[3]["api_content"] == "  exact provider answer\n"
    finally:
        reader.close()


@pytest.mark.parametrize("read_only", [False, True])
def test_replay_preserves_complete_projection_and_display_scope(db, read_only):
    db.create_session("root", source="cli")
    _archive(db, "root", 20)
    _live(db, "root", "root")
    db.end_session("root", end_reason="compression")
    db.create_session("tip", source="cli", parent_session_id="root")
    _archive(db, "tip", 30)
    _live(db, "tip", "tip")
    # A rewound row is audit-visible, never model- or compaction-display-visible.
    row_id = db.append_message("tip", "assistant", "rewound", timestamp=50.0)
    db._write_sql("UPDATE messages SET active=0 WHERE id=?", (row_id,))

    reader = SessionDB(db.db_path, read_only=True) if read_only else db
    try:
        traced = []
        # Read-only uses its own connection; WAL uses the ordinary pooled reader.
        with reader._read_ctx() as conn:
            conn.set_trace_callback(traced.append)
        try:
            active = reader.get_messages_as_conversation("tip", include_row_ids=True)
        finally:
            with reader._read_ctx() as conn:
                conn.set_trace_callback(None)
        active_sql = next(q for q in traced if "FROM messages WHERE session_id IN" in q)
        # Exact former query, on the same complete projection: only its access path differs.
        original_sql = active_sql[:active_sql.rindex("ORDER BY")] + "ORDER BY id"
        with reader._read_ctx() as conn:
            current_rows = conn.execute(active_sql).fetchall()
            original_rows = conn.execute(original_sql).fetchall()
        assert [tuple(r) for r in current_rows] == [tuple(r) for r in original_rows]
        assert [m["_row_id"] for m in active] == [r["id"] for r in original_rows]
        assert active == reader._rows_to_conversation(
            original_rows, session_id="tip", include_ancestors=False,
            repair_alternation=False, include_row_ids=True)
        # The same boundary without physical ids includes CAS snapshots and alternation repair.
        assert reader.get_messages_as_conversation("tip", repair_alternation=True) == reader._rows_to_conversation(
            original_rows, session_id="tip", include_ancestors=False, repair_alternation=True)
        lineage = reader.get_messages_as_conversation("tip", include_ancestors=True, include_row_ids=True)
        assert len(lineage) == 8
        assert [m["_row_id"] for m in lineage] == sorted(m["_row_id"] for m in lineage)
        audit = reader.get_messages_as_conversation("tip", include_inactive=True, include_row_ids=True)
        display = reader.get_messages_as_conversation("tip", include_compacted=True, include_row_ids=True)
        assert len(audit) == 35
        assert len(display) == 34
        assert any(m["content"] == "rewound" for m in audit)
        assert all(m["content"] != "rewound" for m in active + display)
        model_resume, display_resume = reader.get_resume_conversations("tip")
        assert model_resume == reader.get_messages_as_conversation(
            "tip", repair_alternation=True, include_row_ids=True)
        assert display_resume == reader.get_messages_as_conversation(
            "tip", include_ancestors=True, include_compacted=True, include_row_ids=True)

        # A second real writer appends after a compressor's snapshot watermark.
        watermark = active[-1]["_row_id"]
        writer = SessionDB(db.db_path)
        try:
            _live(writer, "tip", "late")
        finally:
            writer.close()
        late = reader.get_messages_as_conversation("tip", include_row_ids=True)[len(active):]
        compacted = [{"role": "user", "content": "[CONTEXT COMPACTION] summary", "timestamp": 400.0}]
        compacted.extend(copy.copy(m) for m in active[1:])
        db.archive_and_compact("tip", compacted, watermark=watermark)
        restored = reader.get_messages_as_conversation("tip", repair_alternation=True)
        assert [m["content"] for m in restored] == [m["content"] for m in compacted + late]
        assert [m["message_uid"] for m in restored[-len(late):]] == [m["message_uid"] for m in late]
        assert [m["timestamp"] for m in restored[-len(late):]] == [300.0, 200.0, 100.0, 100.0]
        assert restored[-3]["tool_calls"][0]["id"] == restored[-2]["tool_call_id"]
    finally:
        if reader is not db:
            reader.close()
