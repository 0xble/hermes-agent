"""Measure compaction row/FTS amplification through the real SQLite write path."""

import re

import pytest

from hermes_state import SessionDB


@pytest.mark.parametrize("named_coverage", [False, True])
def test_compaction_reports_phase_times_and_existing_row_amplification(tmp_path, monkeypatch, caplog, named_coverage):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("topic", source="test")
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
            covered_ids=[msg["_row_id"] for msg in history] if named_coverage else None)
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
                     "callback_total", "commit"):
            assert re.search(rf"{name}=\d+\.\d{{3}}s", warning), warning
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
        if failure:
            def fail(conn):
                db._insert_message_rows(conn, "topic", [{"role": "assistant", "content": "rolled back"}])
                raise ValueError("abort transaction")
            with pytest.raises(ValueError, match="abort transaction"):
                db._execute_write(fail)
            warning = next(record.message for record in caplog.records if "held the write lock" in record.message)
            assert "rollback=" in warning and "inserted=1" in warning
            assert [msg["content"] for msg in db.get_messages("topic")] == ["quiet"]
        assert current_write_metrics.get() is None
        caplog.clear()
        db._execute_write(lambda conn: None)
        warning = next(record.message for record in caplog.records if "held the write lock" in record.message)
        assert "rows[none]" in warning and "commit=" in warning
        assert current_write_metrics.get() is None
    finally:
        db.close()
