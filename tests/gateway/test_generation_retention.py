"""Pruning preserves current-boot consumed scopes and all referenced authority."""
import time

from gateway.generation import GenerationCoordinator


def test_history_pruning_is_bounded_and_keeps_current_boot_and_lease(tmp_path, monkeypatch):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    db = GenerationCoordinator(tmp_path)
    rows = []
    for boot in ['boot', 'prior', 'prior', 'prior']:
        row = db.reserve_generation(release_sha='r', label=f'label-{len(rows)}', boot_id=boot,
                                    started_at=time.time() - 10 * 86400)
        db.claim_generation(row.id, 123, '123:1', boot_id=boot, scope_nonce='consumed')
        rows.append(row)
    db.acquire_lease('active_generation', rows[1].id)
    db.transition_state(rows[1].id, 'standby', 'serving')
    for row in rows:
        db.heartbeat(row.id, state='exited')
    with db.connect() as conn:
        conn.execute('UPDATE generations SET heartbeat_at=1')
    result = db.prune_history(limit=1)
    assert result['generations'] == 1
    assert {row['id'] for row in db.generations()} >= {rows[0].id, rows[1].id}
    assert db.prune_history(limit=1)['generations'] == 1
    assert {row['id'] for row in db.generations()} == {rows[0].id, rows[1].id}
    # N-1's ordinary DELETE also cannot erase a consumed current-boot scope.
    with db.connect() as conn:
        conn.execute('DELETE FROM generations')
    assert {row['id'] for row in db.generations()} == {rows[0].id, rows[1].id}


def test_old_journal_pruning_preserves_current_boot_and_complete_intervals(tmp_path, monkeypatch):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    db = GenerationCoordinator(tmp_path)
    for index, boot in enumerate(['prior', 'boot']):
        row = db.reserve_generation(release_sha='r', label=str(index), boot_id=boot)
        db.claim_generation(row.id, 123, '123:1', boot_id=boot)
        with db.connect() as conn:
            for n, event in enumerate(['lock_acquired', 'poller_started', 'poller_stopped', 'lock_released']):
                conn.execute('INSERT INTO poller_journal '
                    '(generation_id,epoch,token_hash,event,monotonic_at,wall_at,boot_id) VALUES(?,1,?,?,?,?,?)',
                    (row.id, 'hash', event, n, 1, boot))
        db.heartbeat(row.id, state='exited')
    assert db.prune_history(limit=3)['journal'] == 0  # Never truncate an interval.
    assert db.prune_history(limit=4)['journal'] == 4
    with db.connect() as conn:
        assert {row['boot_id'] for row in conn.execute('SELECT * FROM poller_journal')} == {'boot'}
