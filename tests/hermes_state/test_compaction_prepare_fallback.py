"""Compaction publishes despite unrelated writes or an unavailable optimistic reader."""

import sqlite3
import threading

import pytest

from hermes_state import SessionDB
from hermes_state_compaction_prepare import UNRESOLVED_SNAPSHOT_THRESHOLD


@pytest.fixture
def compaction_db(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('topic', source='test')
    db.create_session('other', source='test')
    db.append_messages_batch('topic', [
        {'role': 'user', 'content': 'question'}, {'role': 'assistant', 'content': 'answer'}])
    try:
        yield db
    finally:
        db.close()


def compact(db):
    held = db.get_messages_as_conversation('topic', include_row_ids=True)
    return db.archive_and_compact('topic', [{'role': 'user', 'content': 'summary'}],
        watermark=db.get_active_message_watermark('topic'),
        covered_ids=[m['_row_id'] for m in held], held_messages=held, held_for_cas=held)


@pytest.mark.parametrize('target', ['other', 'topic'])
def test_compaction_publishes_during_concurrent_appends(compaction_db, monkeypatch, target):
    db = compaction_db
    original = db._proved_coverage
    preparations = []
    errors = []

    def prepare(*args):
        result = original(*args)
        locked = db._conn.in_transaction
        preparations.append(locked)
        if not locked:
            def append():
                try:
                    db.append_message(target, 'user', f'concurrent {len(preparations)}')
                except Exception as exc:
                    errors.append(exc)
            writer = threading.Thread(target=append)
            writer.start()
            writer.join(10)
            assert not writer.is_alive(), 'preparation held the writer lock'
            assert not errors
        return result

    monkeypatch.setattr(db, '_proved_coverage', prepare)
    count = compact(db)
    expected_tail = [] if target == 'other' else ['concurrent 1', 'concurrent 2']
    assert count == 1 + len(expected_tail)
    assert [m['content'] for m in db.get_messages('topic')] == ['summary', *expected_tail]
    assert preparations == ([False] if target == 'other' else [False, False, True])


def test_compaction_prepares_in_lock_without_reader(compaction_db, monkeypatch):
    db = compaction_db
    checkout = db._checkout_read_conn
    original = db._proved_coverage
    preparations = []

    def prepare(*args):
        preparations.append(db._conn.in_transaction)
        return original(*args)

    monkeypatch.setattr(db, '_checkout_read_conn',
                        lambda **kw: None if kw.get('independent') else checkout(**kw))
    monkeypatch.setattr(db, '_proved_coverage', prepare)
    assert compact(db) == 1
    assert preparations == [True]
    assert [m['content'] for m in db.get_messages('topic')] == ['summary']


@pytest.mark.parametrize('persistent', [False, True])
def test_compaction_retries_read_ioerr_then_falls_back(compaction_db, monkeypatch, persistent):
    import hermes_state
    from hermes_state import _READ_ONLY_IOERR_RETRY_ATTEMPTS

    db = compaction_db
    original = db._proved_coverage
    preparations = []
    readers = []
    classified = []
    note_error = hermes_state.note_storage_error

    def classify(path, exc):
        classified.append(exc)
        return note_error(path, exc)

    def prepare(*args):
        locked = db._conn.in_transaction
        preparations.append(locked)
        if not locked:
            readers.append(args[0])
        if not locked and (persistent or len(preparations) == 1):
            raise sqlite3.OperationalError('disk I/O error')
        return original(*args)

    monkeypatch.setattr(db, '_proved_coverage', prepare)
    monkeypatch.setattr(hermes_state, 'note_storage_error', classify)
    assert compact(db) == 1
    expected = [False] * (_READ_ONLY_IOERR_RETRY_ATTEMPTS + 1) + [True] if persistent else [False, False]
    assert preparations == expected
    assert len(set(readers)) == 1, 'IOERR retries reopened the pooled reader'
    assert len(classified) == int(persistent)
    assert [m['content'] for m in db.get_messages('topic')] == ['summary']


def test_coverage_null_role_matches_sql_semantics(compaction_db):
    db = compaction_db
    # Legacy stores may have nullable roles. The indexed proof must agree with role = NULL.
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT, active INTEGER)')
        conn.execute("INSERT INTO messages VALUES (1, 'topic', NULL, 'legacy', 1)")
        unresolved = {'role': None, 'content': 'legacy'}
        assert db._matching_active_ids(conn, 'topic', unresolved) == []
        assert db._proved_coverage(conn, 'topic', [], [unresolved]) == ([], set())


@pytest.mark.parametrize('unresolved_count', [
    0, 1, UNRESOLVED_SNAPSHOT_THRESHOLD, UNRESOLVED_SNAPSHOT_THRESHOLD + 1])
def test_lease_preparation_only_snapshots_expensive_coverage(compaction_db, monkeypatch, unresolved_count):
    db = compaction_db
    assert db.try_acquire_compression_lock('topic', 'holder')
    original = db._proved_coverage
    preparations = []

    def prepare(*args):
        preparations.append(db._conn.in_transaction)
        return original(*args)

    monkeypatch.setattr(db, '_proved_coverage', prepare)
    assert db.archive_and_compact('topic', [{'role': 'user', 'content': 'summary'}],
        lock_holder='holder', covered_ids=[], unresolved_held=[
            {'role': 'user', 'content': 'never persisted'} for _ in range(unresolved_count)]) == 3
    assert preparations == [unresolved_count <= UNRESOLVED_SNAPSHOT_THRESHOLD]
    assert [m['content'] for m in db.get_messages('topic')] == ['summary', 'question', 'answer']
