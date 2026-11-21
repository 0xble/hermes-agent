"""Measure compaction row/FTS amplification through the real SQLite write path."""

import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_state import SessionDB


@pytest.mark.parametrize("named_coverage", [False, True])
def test_compaction_reports_phase_times_and_existing_row_amplification(tmp_path, monkeypatch, caplog, named_coverage):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("topic", source="test")
        db.append_messages_batch("topic", [
            {"role": "user", "content": f"amplification historical {i}"} for i in range(4)])
        historical_ids = [row[0] for row in db._read_all("SELECT id FROM messages ORDER BY id")]
        db._write_sql("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ?", ("topic",))
        db._write_sql("UPDATE messages SET display_identity = NULL WHERE id = ?", (historical_ids[0],))
        # This row belongs to BOTH cursors, so enumeration counts must not be mistaken for unique rows.
        db._write_sql("UPDATE messages SET display_order = NULL WHERE id = ?", (historical_ids[1],))
        visible_rows = []
        original_index = db._visible_display_index

        def observe_index(conn, session_id):
            visible_rows.extend(conn.execute(
                "SELECT id, display_identity, display_order FROM messages "
                "WHERE session_id = ? AND (active = 1 OR compacted = 1) ORDER BY id", (session_id,)))
            indexed = original_index(conn, session_id)
            assert indexed == [tuple(row) for row in visible_rows]
            return indexed

        monkeypatch.setattr(db, "_visible_display_index", observe_index)
        # Keep the full-index fallback diagnostics covered. Eligible incremental
        # enumeration and its fixed work bound are exercised by the closure suite.
        monkeypatch.setattr(db, "_begin_display_fold", lambda conn, plan: plan)
        for i in range(12):
            db.append_message("topic", "user" if i % 2 == 0 else "assistant", f"amplification original {i}")
        history = db.get_messages_as_conversation("topic", include_row_ids=True)
        carried = history[-8:]
        watermark = db.get_active_message_watermark("topic")
        for i in range(2):
            db.append_message("topic", "user" if i % 2 == 0 else "assistant", f"amplification concurrent {i}")
        summary = {"role": "user", "content": "amplification summary", "_compressed_summary": True}
        before = db._read_one("SELECT COUNT(*) FROM messages")[0]
        # MATCH reads the actual inverted indexes. COUNT(*) without MATCH on an external-content
        # FTS table just counts its source view, which would hide a missing trigger/index entry.
        fts_before = [db._read_one(f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH 'amplification'")[0]
                      for table in ("messages_fts", "messages_fts_trigram")]
        caplog.clear()
        monkeypatch.setattr(db, "_WRITE_LOCK_WARN_S", 0.0)
        count = db.archive_and_compact(
            "topic", [summary, *carried], watermark=watermark, tail_count=len(carried),
            covered_ids=[msg["_row_id"] for msg in history] if named_coverage else None,
            held_messages=history, model_config_patch={"diagnostic_fixture": True})
        written = db._read_one("SELECT COUNT(*) FROM messages")[0] - before
        fts_written = [db._read_one(f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH 'amplification'")[0] - old
                       for table, old in zip(("messages_fts", "messages_fts_trigram"), fts_before)]
        print(f"carried={len(carried)} concurrent=2 message_inserts={written} fts_inserts={fts_written}")
        # The warning must report every row the write actually added, including carried/concurrent
        # rows that compaction reinserts today. Compare reported counts with the observed delta
        # rather than pinning the delta, so reusing unchanged rows later still passes.
        assert fts_written == [written, written]
        assert count == 11
        warning = next(record.message for record in caplog.records if "held the write lock" in record.message)
        for name in ("archive_mark_inactive", "insert_incl_fts", "clone_incl_fts", "display_reconcile",
                     "display_index", "display_planned_fold", "display_stale_decode",
                     "display_identity_enumerate", "display_backfill_enumerate", "display_materialize",
                     "display_identity_enumerate_cpu", "display_backfill_enumerate_cpu", "display_materialize_cpu",
                     "compaction_prepare", "archive_enumerate", "archive_timestamp_carry", "session_counters",
                     "callback_total", "commit"):
            assert re.search(rf"{name}=\d+\.\d{{3}}s", warning), warning
        rows_match = re.search(r"rows\[([^]]+)\]", warning)
        assert rows_match, warning
        row_counts = {name: int(count) for name, count in re.findall(r"(\w+)=(\d+)", rows_match.group(1))}
        assert row_counts["display_identity_rows"] == sum(row[1] is not None for row in visible_rows)
        assert row_counts["display_backfill_rows"] == sum(
            row[1] is None or row[2] is None for row in visible_rows)
        assert row_counts["display_materialized_rows"] == len(visible_rows) > len([summary, *carried])
        assert (row_counts["display_identity_rows"] + row_counts["display_backfill_rows"]
                > row_counts["display_materialized_rows"])
        inserted_match = re.search(r"inserted=(\d+)", warning)
        cloned_match = re.search(r"cloned=(\d+)", warning)
        assert inserted_match and cloned_match, warning
        assert int(inserted_match.group(1)) + int(cloned_match.group(1)) == written, warning
        for count in ("archived=4", "rewound=10"):
            assert count in warning
        assert "fts=inline" in warning
        assert [msg["content"] for msg in db.get_messages("topic")] == [
            summary["content"], *[msg["content"] for msg in carried],
            "amplification concurrent 0", "amplification concurrent 1"]
    finally:
        db.close()


@pytest.mark.parametrize("failure", [False, True])
def test_write_metrics_obey_threshold_and_do_not_leak_across_attempts(tmp_path, monkeypatch, caplog, failure):
    from hermes_state_write_metrics import current_write_metrics

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("topic", source="test")
        monkeypatch.setattr(db, "_WRITE_LOCK_WARN_S", float("inf"))
        caplog.clear()
        db.append_messages_batch("topic", [{"role": "user", "content": "quiet"}])
        assert not any("held the write lock" in record.message for record in caplog.records)
        monkeypatch.setattr(db, "_WRITE_LOCK_WARN_S", 0.0)
        import hermes_state_write_metrics as metrics_module
        clock = SimpleNamespace(monotonic=Mock(side_effect=[10.0, 15.0]),
                                thread_time=Mock(side_effect=[2.0, 3.0]))
        monkeypatch.setattr(metrics_module, "time", clock)

        def measured(conn):
            with metrics_module.write_phase("enumeration", thread_cpu=True):
                cursor = conn.execute("UPDATE messages SET content = ? WHERE session_id = ?", ("measured", "topic"))
                metrics_module.add_write_rows("measured", cursor.rowcount)
                if failure:
                    raise ValueError("abort transaction")

        if failure:
            with pytest.raises(ValueError, match="abort transaction"):
                db._execute_write(measured)
        else:
            db._execute_write(measured)
        warning = next(record.message for record in caplog.records if "held the write lock" in record.message)
        assert "enumeration=5.000s" in warning and "enumeration_cpu=1.000s" in warning
        assert "measured=1" in warning
        assert f"outcome={'rolled_back' if failure else 'committed'}" in warning
        assert [msg["content"] for msg in db.get_messages("topic")] == ["quiet" if failure else "measured"]
        assert current_write_metrics.get() is None
        with metrics_module.write_phase("no_context", thread_cpu=True):
            pass
        assert clock.monotonic.call_count == clock.thread_time.call_count == 2
        caplog.clear()
        db._execute_write(lambda conn: None)
        warning = next(record.message for record in caplog.records if "held the write lock" in record.message)
        assert "rows[none]" in warning and "commit=" in warning and "enumeration" not in warning
        assert current_write_metrics.get() is None
    finally:
        db.close()


@pytest.mark.parametrize("named_coverage", [False, True])
@pytest.mark.parametrize("failure_phase", ["compaction_prepare", "session_counters"])
def test_compaction_failure_phases_rollback_and_remain_attempt_local(
    tmp_path, monkeypatch, caplog, named_coverage, failure_phase,
):
    from hermes_state_errors import CompressionLeaseLostError
    from hermes_state_write_metrics import current_write_metrics

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("topic", source="test")
        db.append_messages_batch("topic", [
            {"role": "user", "content": "original"}, {"role": "assistant", "content": "reply"}])
        history = db.get_messages_as_conversation("topic", include_row_ids=True)
        before = [tuple(row) for row in db._read_all("SELECT * FROM messages ORDER BY id")]
        counters = db._read_one("SELECT message_count, tool_call_count FROM sessions WHERE id = 'topic'")
        assert counters is not None
        counters = tuple(counters)
        if failure_phase == "session_counters":
            db._write_sql("CREATE TRIGGER reject_counters BEFORE UPDATE OF message_count ON sessions "
                          "BEGIN SELECT RAISE(ABORT, 'reject counters'); END")
        monkeypatch.setattr(db, "_WRITE_LOCK_WARN_S", 0.0)
        caplog.clear()
        summary = {"role": "user", "content": "private rollback summary", "_compressed_summary": True}
        with pytest.raises(CompressionLeaseLostError if failure_phase == "compaction_prepare" else sqlite3.IntegrityError):
            db.archive_and_compact(
                "topic", [summary], watermark=history[-1]["_row_id"],
                lock_holder="missing-holder" if failure_phase == "compaction_prepare" else None,
                covered_ids=[msg["_row_id"] for msg in history] if named_coverage else None)
        warning = next(record.message for record in caplog.records if "held the write lock" in record.message)
        assert re.search(rf"{failure_phase}=\d+\.\d{{3}}s", warning), warning
        assert "outcome=rolled_back" in warning and "rollback=" in warning
        assert "private rollback summary" not in warning
        if failure_phase == "session_counters":
            assert "inserted=1" in warning and "display_materialized_rows=" in warning
        assert [tuple(row) for row in db._read_all("SELECT * FROM messages ORDER BY id")] == before
        settled = db._read_one("SELECT message_count, tool_call_count FROM sessions WHERE id = 'topic'")
        assert settled is not None and tuple(settled) == counters
        assert "_row_id" not in summary and "timestamp" not in summary
        assert current_write_metrics.get() is None
        caplog.clear()
        db._execute_write(lambda conn: None)
        warning = next(record.message for record in caplog.records if "held the write lock" in record.message)
        assert "rows[none]" in warning and failure_phase not in warning
        assert current_write_metrics.get() is None
    finally:
        db.close()
