"""Exhausted notification history cannot grow without a retention owner."""
import queue
import json
import time

import pytest

from tools import async_delegation as ad


def exhausted(uid, now, metadata=None, status="completed"):
    ad._persist_dispatch({'delegation_id': uid, 'session_key': 'unavailable',
                          'dispatched_at': now, **({'delegation_metadata': metadata} if metadata else {})})
    ad._persist_completion({'type': 'async_delegation', 'delegation_id': uid,
                           'status': status, 'summary': uid, 'completed_at': now},
                          {'summary': uid})
    for cycle in range(ad._MAX_DELIVERY_RECOVERIES + 1):
        if cycle:
            assert ad.recover_completion_delivery(uid)
        for attempt in range(ad._MAX_DELIVERY_ATTEMPTS):
            claim = f'{uid}-{cycle}-{attempt}'
            assert ad.claim_completion_delivery(uid, claim)
            assert ad.release_completion_delivery(uid, claim)


@pytest.mark.parametrize('status', ['completed', 'budget_exhausted'])
@pytest.mark.parametrize('pruning', ['age', 'capacity'])
def test_exhausted_unlabelled_completion_history_is_bounded(tmp_path, monkeypatch, pruning, status):
    monkeypatch.setattr(ad, '_db_path', lambda: tmp_path / 'async.db')
    monkeypatch.setattr(ad, '_MAX_DELIVERY_ATTEMPTS', 1)
    monkeypatch.setattr(ad, '_MAX_RETAINED_COMPLETED', 2)
    clock = [time.time()]
    monkeypatch.setattr(ad.time, 'time', lambda: clock[0])
    for i in range(4):
        clock[0] += 1
        exhausted(f'legacy-{i}', clock[0], status=status)
    assert ad.retry_exhausted_completions() == []
    assert ad.restore_undelivered_completions(queue.Queue()) == 0
    if pruning == 'age':
        clock[0] += ad._DURABLE_RETENTION_SECONDS + 1
    ad._prune_durable_records()
    with ad._transaction() as conn:
        rows = conn.execute("SELECT delegation_id, delivery_state FROM async_delegations").fetchall()
    count = len(rows)
    assert all(state == 'dropped' for _, state in rows)
    assert count == (0 if pruning == 'age' else ad._MAX_RETAINED_COMPLETED)
    if pruning == 'capacity':
        assert {uid for uid, _ in rows} == {'legacy-2', 'legacy-3'}


@pytest.mark.parametrize('status', ['completed', 'budget_exhausted'])
@pytest.mark.parametrize('protection', [
    'retained_card', 'owner_projection', 'label_projection', 'event_owner', 'result_label',
    'malformed_task', 'malformed_result', 'malformed_event', 'unknown_metadata',
    'running', 'finalizing', 'stalling', 'unknown_state', 'live_claim', 'unused_recovery', 'unused_attempts',
])
def test_exhaustion_pruning_preserves_uncertain_or_recoverable_work(tmp_path, monkeypatch, protection, status):
    monkeypatch.setattr(ad, '_db_path', lambda: tmp_path / 'async.db')
    monkeypatch.setattr(ad, '_MAX_DELIVERY_ATTEMPTS', 1)
    monkeypatch.setattr(ad, '_MAX_RETAINED_COMPLETED', 0)
    clock = [time.time()]
    monkeypatch.setattr(ad.time, 'time', lambda: clock[0])
    metadata = {'parent_task_id': 'a' * 32, 'owner': {'session_id': 'parent'},
                'threads': [{'thread_ref': 'A', 'task_label': 'Task A'}]} if protection == 'retained_card' else None
    if metadata:
        metadata['owner_json'] = json.dumps(metadata['owner'], sort_keys=True, separators=(',', ':'))
    exhausted('protected', clock[0], metadata, status=status)
    changes = {
        'owner_projection': ('owner_json', '{"session_id":"parent"}'),
        'label_projection': ('task_label', 'Task A'),
        'event_owner': ('event_json', '{"owner":{"session_id":"parent"}}'),
        'result_label': ('result_json', '{"results":[{"task_label":"Task A"}]}'),
        'malformed_task': ('task_json', '{'),
        'malformed_result': ('result_json', '[]'),
        'malformed_event': ('event_json', 'null'),
        'unknown_metadata': ('task_json', '{"delegation_metadata":{"legacy":"unknown"}}'),
        'running': ('state', 'running'), 'finalizing': ('state', 'finalizing'), 'stalling': ('state', 'stalling'),
        'unknown_state': ('state', 'unknown'),
        'live_claim': ('delivery_claim', 'held'),
        'unused_recovery': ('delivery_recovery_attempts', 0),
        'unused_attempts': ('delivery_attempts', 0),
    }
    if protection in changes:
        column, value = changes[protection]
        with ad._transaction() as conn:
            conn.execute(f'UPDATE async_delegations SET {column}=? WHERE delegation_id=?', (value, 'protected'))
    clock[0] += ad._DURABLE_RETENTION_SECONDS + 1
    with ad._transaction() as conn:
        before = conn.execute('SELECT * FROM async_delegations WHERE delegation_id=?', ('protected',)).fetchone()
    ad._prune_durable_records()
    with ad._transaction() as conn:
        after = conn.execute('SELECT * FROM async_delegations WHERE delegation_id=?', ('protected',)).fetchone()
    assert after == before
    if protection == 'retained_card':
        assert ad.get_delegation_result('protected', owner=metadata['owner'])['result'] == {'summary': 'protected'}
    if protection == 'unused_recovery':
        assert ad.recover_completion_delivery('protected')


def test_pruner_locks_eligibility_snapshot_against_second_connection(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(ad, '_db_path', lambda: tmp_path / 'async.db')
    monkeypatch.setattr(ad, '_MAX_DELIVERY_ATTEMPTS', 1)
    exhausted('contended', time.time())
    original = ad._has_retained_result
    checked = []
    contender = sqlite3.connect(tmp_path / 'async.db', timeout=0)
    def race(task, result):
        if not checked:
            try:
                contender.execute("UPDATE async_delegations SET owner_json=? WHERE delegation_id=?",
                                  ('{"owner":"new"}', 'contended'))
                contender.commit()
            except sqlite3.OperationalError as exc:
                contender.rollback()
                checked.append(str(exc))
            else:
                checked.append('concurrent metadata update committed')
        return original(task, result)
    monkeypatch.setattr(ad, '_has_retained_result', race)
    try:
        ad._prune_durable_records()
        assert checked == ['database is locked']
        # The claim is transaction-scoped, not a leaked cross-process lock.
        contender.execute("UPDATE async_delegations SET owner_json=? WHERE delegation_id=?",
                          ('{"owner":"after"}', 'contended'))
        contender.commit()
        assert contender.execute("SELECT owner_json FROM async_delegations WHERE delegation_id='contended'").fetchone() == ('{"owner":"after"}',)
    finally:
        contender.close()


@pytest.mark.parametrize('pruning', ['age', 'capacity'])
def test_known_owner_abandoned_outcome_enters_exhausted_history(tmp_path, monkeypatch, pruning):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(ad, '_db_path', lambda: tmp_path / 'async.db')
    monkeypatch.setattr(ad, '_MAX_DELIVERY_ATTEMPTS', 1)
    monkeypatch.setattr(ad, '_MAX_RETAINED_COMPLETED', 0)
    ad._persist_dispatch({'delegation_id': 'abandoned', 'session_key': 'unavailable',
                          'dispatched_at': time.time()})
    # Null owner means no process to probe or terminate.
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET owner_pid=NULL WHERE delegation_id='abandoned'")
    assert ad.recover_abandoned_delegations() == 1
    for cycle in range(ad._MAX_DELIVERY_RECOVERIES + 1):
        if cycle:
            assert ad.recover_completion_delivery('abandoned')
        assert ad.claim_completion_delivery('abandoned', 'claim')
        assert ad.release_completion_delivery('abandoned', 'claim')
    if pruning == 'age':
        monkeypatch.setattr(ad, '_MAX_RETAINED_COMPLETED', 50)
        with ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET updated_at=0 WHERE delegation_id='abandoned'")
    ad._prune_durable_records()
    assert ad.get_durable_delegation('abandoned') is None
