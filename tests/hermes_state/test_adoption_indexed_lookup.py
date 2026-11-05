"""Adoption must avoid wide history reads without weakening its atomic checks."""
import itertools
import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_state import SessionDB
import hermes_state_runtime as rt
from tests.fakes.runtime_owner import begin_runtime_epoch


@pytest.fixture
def worker(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('s', source='test')
    epoch = begin_runtime_epoch(db, instance_id='boot')
    accepted = rt.admit_session_input(db, epoch=epoch, principal_id='human',
        session_id='s', request_id='input', payload={})
    claim = rt.claim_session_input(db, epoch=epoch, session_id='s')
    assignment = dict(execution_id='worker', session_id='s', generation=claim['generation'])
    rt.register_worker_execution(db, epoch=epoch, **assignment, kind='compute', adoption_secret='private')
    try:
        yield db, epoch, assignment, accepted['admission_id']
    finally:
        db.close()


def adoption_trace(worker):
    db, epoch, assignment, _ = worker
    statements = []
    db._conn.set_trace_callback(statements.append)
    try:
        rt.adopt_worker_execution(db, epoch=epoch, **assignment, adoption_secret='private')
    finally:
        db._conn.set_trace_callback(None)
    begin = next(i for i, sql in enumerate(statements) if sql.upper() == 'BEGIN IMMEDIATE')
    commit = next(i for i in range(begin, len(statements)) if statements[i].upper() == 'COMMIT')
    return statements[begin:commit + 1]


def test_adoption_candidate_plan_uses_covering_index_not_admissions_table_scan(worker):
    db, _, _, _ = worker
    statements = adoption_trace(worker)
    query, = [sql for sql in statements if 'json_each(session_admissions.lineage_json)' in sql]
    plan = [row[3] for row in db._conn.execute('EXPLAIN QUERY PLAN ' + query)]
    assert not any(detail == 'SCAN session_admissions' for detail in plan), plan
    assert any('USING COVERING INDEX session_admissions_pending' in detail for detail in plan), plan
    assert any('SEARCH session_admissions USING INTEGER PRIMARY KEY' in detail for detail in plan), plan


def test_adoption_session_revalidations_fetch_only_named_generation_column(worker):
    statements = adoption_trace(worker)
    queries = [sql for sql in statements if 'FROM sessions WHERE id=' in sql]
    assert len(queries) == 2, queries
    assert all(sql.startswith('SELECT runtime_generation FROM sessions WHERE id=') for sql in queries), queries


def test_linked_admission_matches_original_query_for_1050_seeded_cases(tmp_path):
    rng = random.Random(4125)
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        sessions = ('s', 'child', 'other', 'unrelated')
        for sid in sessions:
            db.create_session(sid, source='test')
        rows = []
        for i in range(1000):
            status = rng.choice(('queued', 'started', 'unknown', 'terminal'))
            generation = rng.choice((0, 1, 2) if status == 'started' else (None, 0, 1, 2))
            epoch = rng.choice((1, 2, 3) if status == 'started' else (None, 1, 2, 3))
            lineage = rng.choice(([], ['s'], ['s', 'child'], ['other'], ['s', 's'], ['missing']))
            rows.append((f'a{i}', f'r{i}', 'human', rng.choice(sessions), json.dumps(lineage),
                         '{}', 'digest', 'queue', status, epoch, generation))
        db._execute_write(lambda c: c.executemany('''INSERT INTO session_admissions
            (admission_id,request_id,principal_id,target_session_id,lineage_json,payload_json,
             payload_digest,intent,status,owner_epoch,generation) VALUES(?,?,?,?,?,?,?,?,?,?,?)''', rows))
        cases = itertools.product((*sessions, 'missing', '', 'ancestor'),
            (None, 0, 1, 2, 99), (None, 1, 2, 3, 99),
            ((), ('started',), ('unknown',), ('queued', 'terminal'),
             ('started', 'unknown'), ('terminal', 'started', 'started', 'queued', 'unknown')))
        count = 0
        for sid, generation, epoch, statuses in cases:
            placeholders = ','.join('?' for _ in statuses)
            expected = db._conn.execute(f'''SELECT * FROM session_admissions
                WHERE generation=? AND owner_epoch=? AND status IN ({placeholders})
                  AND (target_session_id=? OR EXISTS (
                      SELECT 1 FROM json_each(session_admissions.lineage_json) WHERE value=?
                  )) ORDER BY seq''', (generation, epoch, *statuses, sid, sid)).fetchall()
            statements = []
            db._conn.set_trace_callback(statements.append)
            try:
                if len(expected) > 1:
                    with pytest.raises(rt.RuntimeStoreError, match='admission_conflict'):
                        rt._linked_worker_admission(db._conn, sid, generation, epoch, statuses)
                else:
                    actual = rt._linked_worker_admission(db._conn, sid, generation, epoch, statuses)
                    assert (dict(actual) if actual else None) == (dict(expected[0]) if expected else None)
            finally:
                db._conn.set_trace_callback(None)
            actual_rows = db._conn.execute(statements[-1]).fetchall()
            assert [tuple(row) for row in actual_rows] == [tuple(row) for row in expected]
            count += 1
        assert count == 1050
    finally:
        db.close()


def test_adoption_and_concurrent_resolution_cannot_rearm_terminal_admission(worker):
    db, epoch, assignment, admission_id = worker
    epoch = begin_runtime_epoch(db, instance_id='replacement')
    rt.recover_session_inputs(db, epoch=epoch)
    barrier = threading.Barrier(2)

    def compete(adopt):
        other = SessionDB(db_path=db.db_path)
        try:
            barrier.wait(timeout=10)
            if adopt:
                try:
                    rt.adopt_worker_execution(other, epoch=epoch, **assignment, adoption_secret='private')
                    return 'adopted'
                except rt.RuntimeStoreError as exc:
                    assert str(exc) == 'stale_generation'
                    return 'rejected'
            try:
                rt.resolve_unknown_session_input(other, epoch=epoch, admission_id=admission_id,
                    generation=assignment['generation'])
                return 'resolved'
            except rt.RuntimeStoreError as exc:
                assert str(exc) == 'stale_generation'
                return 'rejected'
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, adopt) for adopt in (True, False)]
        results = [future.result(timeout=20) for future in futures]
    admission = rt.get_session_admission(db, admission_id=admission_id)
    worker_row = db._conn.execute('SELECT status,owner_epoch FROM worker_executions WHERE execution_id=?',
                                 (assignment['execution_id'],)).fetchone()
    assert results in (['adopted', 'rejected'], ['rejected', 'resolved'])
    assert (admission['status'], worker_row['status']) == (
        ('started', 'running') if results[0] == 'adopted' else ('terminal', 'terminal'))


@pytest.mark.parametrize('invalid,reason', [
    ('missing', 'not_found'), ('retired', 'not_found'), ('generation', 'stale_generation'),
    ('epoch', 'stale_epoch'), ('secret', 'permission_denied'), ('terminal', 'stale_generation'),
    ('reserved_epoch', 'stale_epoch'),
])
def test_adoption_preserves_validation_failures(worker, invalid, reason):
    db, epoch, assignment, _ = worker
    args = dict(epoch=epoch, **assignment, adoption_secret='private')
    if invalid == 'missing':
        db._execute_write(lambda c: c.execute("DELETE FROM worker_executions"))
        db._execute_write(lambda c: c.execute("DELETE FROM session_admissions"))
        db._execute_write(lambda c: c.execute("DELETE FROM sessions WHERE id='s'"))
        # Keep the original worker row, so the session lookup itself must refuse it.
        db._conn.execute('PRAGMA foreign_keys=OFF')
        db._execute_write(lambda c: c.execute('''INSERT INTO worker_executions
            (execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest)
            VALUES('worker','s','compute',?,?,'registered',?)''',
            (epoch, assignment['generation'], rt._secret_digest('private'))))
        db._conn.execute('PRAGMA foreign_keys=ON')
    elif invalid == 'retired':
        from hermes_state_mutation_retirement import RETIRED_PREFIX
        db._execute_write(lambda c: c.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                                             (RETIRED_PREFIX + 's', '{}')))
    elif invalid == 'generation':
        db._execute_write(lambda c: c.execute("UPDATE sessions SET runtime_generation=runtime_generation+1 WHERE id='s'"))
    elif invalid == 'epoch':
        args['epoch'] += 1
    elif invalid == 'secret':
        args['adoption_secret'] = 'wrong'
    elif invalid == 'terminal':
        db._execute_write(lambda c: c.execute("UPDATE worker_executions SET status='terminal' WHERE execution_id='worker'"))
    else:
        args['epoch'] = begin_runtime_epoch(db, instance_id='replacement')
        args['execution_id'] = 'admission-worker:reserved'
        db._execute_write(lambda c: c.execute("UPDATE worker_executions SET execution_id=? WHERE execution_id='worker'",
                                             (args['execution_id'],)))
    with pytest.raises(rt.RuntimeStoreError, match=reason):
        rt.adopt_worker_execution(db, **args)


def test_admission_cas_failure_rolls_back_worker_adoption(worker):
    db, epoch, assignment, admission_id = worker
    db._execute_write(lambda c: c.execute('''CREATE TRIGGER refuse_adoption
        BEFORE UPDATE OF owner_epoch ON session_admissions BEGIN SELECT RAISE(IGNORE); END'''))
    with pytest.raises(rt.RuntimeStoreError, match='stale_generation'):
        rt.adopt_worker_execution(db, epoch=epoch, **assignment, adoption_secret='private')
    assert rt.get_session_admission(db, admission_id=admission_id)['status'] == 'started'
    assert db._conn.execute('SELECT status FROM worker_executions WHERE execution_id=?',
                            (assignment['execution_id'],)).fetchone()[0] == 'registered'
