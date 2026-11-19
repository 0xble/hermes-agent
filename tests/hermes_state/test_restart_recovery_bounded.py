"""Restart recovery must not hold the state.db writer for a scan over history.

At every gateway start ``recover_session_inputs`` scanned the whole wide admissions table (19-20 s)
and ``release_handed_off_receipts`` read every local receipt inside the write lock (30-70 s) while
finding nothing to release, so no message, receipt or lease could be written for ~50 s.
"""
import json
import re

import pytest

import hermes_state_runtime as rt
from gateway.session_local_recovery import release_handed_off_receipts
from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX
from tests.fakes.runtime_owner import begin_runtime_epoch


@pytest.fixture
def db(tmp_path):
    handle = SessionDB(db_path=tmp_path / 'state.db')
    try:
        yield handle
    finally:
        handle.close()


def _traced(db, fn):
    statements = []
    db._conn.set_trace_callback(statements.append)
    try:
        fn()
    finally:
        db._conn.set_trace_callback(None)
    return statements


def _seed_admissions(db, epoch):
    db.create_session('s', source='test')
    rows = []
    for i, (status, outcome, owner) in enumerate([
            ('terminal', 'done', epoch), ('terminal', rt.STEERING, epoch), ('terminal', rt.STEERING, epoch + 1),
            ('started', None, epoch), ('started', None, epoch + 1), ('queued', None, None)] * 20):
        rows.append((f'a{i}', f'r{i}', 'human', 's', '[]', json.dumps({'pad': 'x' * 4096}), 'digest', 'queue',
                     status, outcome, owner, 0 if status == 'started' else None))
    db._execute_write(lambda c: c.executemany(
        'INSERT INTO session_admissions (admission_id, request_id, principal_id, target_session_id, lineage_json, '
        'payload_json, payload_digest, intent, status, outcome, owner_epoch, generation) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', rows))


def test_recovery_reads_only_in_flight_rows_and_keeps_its_effect(db):
    old = begin_runtime_epoch(db, instance_id='old')
    _seed_admissions(db, old)
    epoch = begin_runtime_epoch(db, instance_id='new')
    assert epoch == old + 1

    statements = _traced(db, lambda: rt.recover_session_inputs(db, epoch=epoch))
    updates = [sql for sql in statements if sql.upper().startswith('UPDATE')
               and ('session_admissions' in sql or 'worker_executions' in sql)]
    assert len(updates) == 3, statements
    # Any index over the whole table still visits settled history; only the partial
    # in-flight indexes (a status predicate in their WHERE) keep recovery bounded.
    in_flight = {name for name, sql in db._conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name IN ('session_admissions', 'worker_executions')")
        if sql and re.search(r'\bWHERE\b.*\bstatus\b', sql, re.DOTALL)}
    for sql in updates:
        plan = [row[3] for row in db._conn.execute('EXPLAIN QUERY PLAN ' + sql)]
        touched = [detail for detail in plan
                   if re.match(r'(SCAN|SEARCH) (session_admissions|worker_executions)\b', detail)]
        assert touched, (sql, plan)
        for detail in touched:
            used = re.fullmatch(r'(?:SCAN|SEARCH) \w+ USING (?:COVERING )?INDEX (\w+)(?: \(.*\))?', detail)
            assert used and used.group(1) in in_flight, (sql, plan)

    with db._read_ctx() as conn:
        counts = dict(((status, outcome, owner), n) for status, outcome, owner, n in conn.execute(
            'SELECT status, outcome, owner_epoch, count(*) FROM session_admissions '
            'GROUP BY status, outcome, owner_epoch'))
    assert counts == {
        ('terminal', 'done', old): 20,
        ('terminal', 'interrupted', old): 20,  # a steer handed to a lost claim is never replayed
        ('terminal', rt.STEERING, epoch): 20,
        ('unknown', None, old): 20,
        ('started', None, epoch): 20,
        ('queued', None, None): 20,
    }


def _receipt(db, owner, target, route):
    db.set_meta(POLICY_PREFIX + owner, json.dumps({'entry': {'session_id': target}, 'route': route,
                                                   'lineage': [owner]}))


def _seed_receipts(db, count):
    for i in range(count):
        sid = f'local-{i}'
        db.create_session(sid, source='cli')
        db._write_sql('UPDATE sessions SET session_key = ? WHERE id = ?', (f'agent:main:local:{i}', sid))
        _receipt(db, sid, sid, f'agent:main:local:{i}')


def test_handed_off_receipt_scan_never_takes_the_writer_when_nothing_moved(db, monkeypatch):
    epoch = begin_runtime_epoch(db, instance_id='boot')
    _seed_receipts(db, 50)

    def _writer(*_args, **_kwargs):
        raise AssertionError('the receipt scan must not run inside the write lock')

    monkeypatch.setattr(db, '_execute_write', _writer)
    release_handed_off_receipts(db, epoch=epoch)


def test_handed_off_receipt_is_released_by_a_writer_that_rechecks_only_candidates(db):
    epoch = begin_runtime_epoch(db, instance_id='boot')
    _seed_receipts(db, 20)
    db.create_session('moved', source='telegram')
    db._write_sql('UPDATE sessions SET session_key = ? WHERE id = ?', ('agent:main:telegram:dm:1', 'moved'))
    _receipt(db, 'moved', 'moved', 'agent:main:local:moved')

    statements = _traced(db, lambda: release_handed_off_receipts(db, epoch=epoch))

    assert db.get_meta(POLICY_PREFIX + 'moved') is None
    assert db.get_meta(POLICY_PREFIX + 'local-0') is not None
    begin = statements.index('BEGIN IMMEDIATE')
    in_lock = statements[begin:]
    assert not any('GLOB' in sql for sql in in_lock), in_lock
    assert all('local-' not in sql for sql in in_lock), "the writer re-checks only the moved receipt"
