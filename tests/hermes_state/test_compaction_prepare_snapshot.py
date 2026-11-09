"""Compaction preparation cannot own the writer, and stale snapshots cannot publish."""

from contextlib import closing
from types import SimpleNamespace
import sqlite3
import threading
import time

import pytest

from hermes_state import SessionDB
from hermes_state_publication import publication_metrics


def test_large_compaction_excludes_preparation_from_write_hold(tmp_path, monkeypatch):
    import hermes_state

    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('topic', source='test')
        db.append_messages_batch('topic', [
            {'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'original {i}'}
            for i in range(5000)])
        held = db.get_messages_as_conversation('topic', include_row_ids=True)
        now = [100.0]
        clock = SimpleNamespace(**{key: getattr(time, key) for key in dir(time) if not key.startswith('_')})
        clock.monotonic = lambda: now[0]
        monkeypatch.setattr(hermes_state, 'time', clock)
        real_coverage = db._proved_coverage
        preparations = []
        content_read_counts = []
        writer_available = []
        checks = []
        for method in ('_raise_if_unheld_summary', 'held_row_mismatches'):
            original_check = getattr(db, method)
            def check(*args, original_check=original_check):
                checks.append(db._conn.in_transaction)
                return original_check(*args)
            monkeypatch.setattr(db, method, check)

        def prepare(*args):
            preparations.append(db._conn.in_transaction)
            def probe_writer():
                acquired = db._lock.acquire(blocking=False)
                writer_available.append(acquired)
                if acquired:
                    db._lock.release()
            probe = threading.Thread(target=probe_writer)
            probe.start()
            probe.join(10)
            assert not probe.is_alive()
            now[0] += 7.0  # Deterministic CPU cost, independent of machine load.
            statements = []
            args[0].set_trace_callback(statements.append)
            try:
                result = real_coverage(*args)
            finally:
                args[0].set_trace_callback(None)
            content_reads = [sql for sql in statements if sql.startswith('SELECT id') and 'FROM messages' in sql]
            content_read_counts.append(len(content_reads))
            return result

        monkeypatch.setattr(db, '_proved_coverage', prepare)
        with publication_metrics(30) as metrics:
            db.archive_and_compact('topic', [{'role': 'user', 'content': 'summary'}],
                covered_ids=[], unresolved_held=[{k: v for k, v in m.items() if k != '_row_id'} for m in held],
                held_messages=held, held_for_cas=held)
        assert metrics['publish_hold_ms'] == 0, 'preparation time was charged to the write lock'
        assert checks == [False, False], 'held-history or CAS comparison owned the write lock'
        assert preparations == [False], 'preparation ran inside BEGIN IMMEDIATE'
        assert writer_available == [True], 'preparation blocked same-process writers'
        assert content_read_counts == [1], 'coverage repeatedly scanned active content'
        assert [m['content'] for m in db.get_messages('topic')] == ['summary']
    finally:
        db.close()


@pytest.mark.parametrize('mutation', ['append', 'external_append', 'rewrite', 'always_append', 'lease_loss'])
def test_compaction_reprepares_or_refuses_changed_snapshot(tmp_path, monkeypatch, mutation):
    from hermes_state_errors import CompressionLeaseLostError, HeldRowRewrittenError

    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('topic', source='test')
        db.append_messages_batch('topic', [
            {'role': 'user', 'content': 'question'}, {'role': 'assistant', 'content': 'answer'}])
        held = db.get_messages_as_conversation('topic', include_row_ids=True)
        watermark = db.get_active_message_watermark('topic')
        original = db._proved_coverage
        attempts = []
        fold_selections = []
        original_fold = db._begin_display_fold
        def begin_fold(conn, plan):
            result = original_fold(conn, plan)
            fold_selections.append(type(result).__name__)
            return result
        monkeypatch.setattr(db, '_begin_display_fold', begin_fold)
        if mutation == 'lease_loss':
            assert db.try_acquire_compression_lock('topic', 'holder')

        def prepare(*args):
            result = original(*args)
            attempts.append(True)
            assert not db._conn.in_transaction, 'concurrent writes are blocked during preparation'
            if len(attempts) == 1 or mutation == 'always_append':
                if mutation == 'lease_loss':
                    db.release_compression_lock('topic', 'holder')
                elif mutation == 'external_append':
                    with closing(sqlite3.connect(db.db_path)) as other, other:
                        other.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                                      ('topic', 'user', 'concurrent 1', 1700000000))
                elif mutation == 'rewrite':
                    db.set_user_message_content('topic', held[0]['_row_id'], 'edited question')
                else:
                    db.append_message('topic', 'user', f'concurrent {len(attempts)}')
            return result

        monkeypatch.setattr(db, '_proved_coverage', prepare)
        summary = {'role': 'user', 'content': 'summary'}
        def compact():
            return db.archive_and_compact('topic', [summary], watermark=watermark,
                lock_holder='holder' if mutation == 'lease_loss' else None,
                covered_ids=[m['_row_id'] for m in held], held_messages=held, held_for_cas=held)

        if mutation == 'lease_loss':
            with pytest.raises(CompressionLeaseLostError):
                compact()
            assert len(attempts) == 1
        elif mutation == 'rewrite':
            with pytest.raises(HeldRowRewrittenError):
                compact()
            assert db.get_messages('topic')[0]['content'] == 'edited question'
        elif mutation == 'always_append':
            with pytest.raises(RuntimeError, match='changed.*prepar'):
                compact()
            assert len(attempts) == 3
            assert '_row_id' not in summary
            assert db.get_messages('topic')[0]['content'] == 'question'
        else:
            assert compact() == 2
            assert len(attempts) == 2
            assert fold_selections == ['_AffectedFold'], 'retry reused stale display preparation'
            assert [m['content'] for m in db.get_messages('topic')] == ['summary', 'concurrent 1']
    finally:
        db.close()
