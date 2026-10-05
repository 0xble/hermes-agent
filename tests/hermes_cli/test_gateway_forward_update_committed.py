"""Committed-holder fault recovery, using the real coordinator and pointer rig."""
import json

import pytest

from hermes_cli import immutable_releases as releases
from hermes_cli import gateway_forward_update as forward
from tests.hermes_cli.test_gateway_forward_update import promote, rig  # noqa: F401


@pytest.mark.parametrize('caller,fault', [
    (caller, fault)
    for caller in ('promote', 'promote_rollback', 'recover', 'recover_committed', 'recover_rollback', 'recover_committed_rollback', 'recover_activated')
    for fault in ('activation_sync', 'activation_verify', 'activation_archive', 'activation_unknown',
                  'commit_save', 'pointer_readback', 'final_pointer_readback', 'owner_readback',
                  'finish_save', 'archive', 'receipt', 'login', 'cleanup', 'rollback_reply',
                  'pointer_inspection', 'polling_io', 'promotion_clock',
                  'pre_flip_clock', 'orphan_cleanup', 'inventory_finish')
    if ((caller != 'recover_activated' or fault in {'pre_flip_clock', 'orphan_cleanup', 'inventory_finish'})
        and (fault not in {'pre_flip_clock', 'orphan_cleanup', 'inventory_finish'} or caller == 'recover_activated')
        and (caller != 'promote_rollback' or fault in {'finish_save', 'archive', 'receipt', 'rollback_reply'})
        and not (fault.startswith('activation_') and 'committed' in caller)
        and (fault != 'rollback_reply' or 'rollback' in caller)
        and (fault not in {'pointer_inspection', 'polling_io'} or 'committed' in caller)
        and (fault != 'promotion_clock' or caller == 'promote'))
])
def test_review10_post_flip_exception_keeps_polling_holder(rig, monkeypatch, fault, caller):
    rollback_owner = 'rollback' in caller
    committed = 'committed' in caller
    expected = rig.a if rollback_owner else rig.b
    if caller not in {'promote', 'promote_rollback'}:
        with monkeypatch.context() as setup:
            if caller == 'recover_activated':
                def crash_before_commit_record(*args):
                    raise KeyboardInterrupt('pointer activated before forward commit record')
                setup.setattr(forward, '_commit_pointer', crash_before_commit_record)
            elif rollback_owner or committed:
                flip = forward._flip
                def crash_flip(*args, **kwargs):
                    if not rollback_owner or kwargs.get('operation') == 'rollback':
                        if committed:
                            flip(*args, **kwargs)
                        raise KeyboardInterrupt('rollback/promotion lease acquired')
                    flip(*args, **kwargs)
                    rig.alive.pop(args[2]['pid'])
                    raise RuntimeError('pointer-committed B died before rollback')
                setup.setattr(forward, '_flip', crash_flip)
            else:
                handover = forward.handover_to_generation
                def crash(*args, **kwargs):
                    handover(*args, **kwargs)
                    raise KeyboardInterrupt('after lease commit before pointer flip')
                setup.setattr(forward, 'handover_to_generation', crash)
            with pytest.raises(KeyboardInterrupt):
                promote(rig)
        rig.supervisor.mode = 'happy'
        intent = json.loads((rig.home / 'forward-update.json').read_text())
        assert (intent.get('pointer_commit', {}).get('generation_id') == rig.db.leases()[0]['generation_id']) == committed
    if fault == 'orphan_cleanup':
        abandoned = rig.db.reserve_generation(release_sha=rig.a.name, label='abandoned-rollback')
        intent['rollback_generation'] = {'id': abandoned.id, 'label': abandoned.label,
            'release_sha': abandoned.release_sha, 'reservation_at': abandoned.started_at}
        forward._save(rig.home, intent)
    rollback_calls, fired, phase = [], [], {'flip': False, 'returned': False, 'bookkeeping': False, 'reads': 0, 'proven': False}
    polling_request = rig.supervisor.request
    def track_polling(*args, **kwargs):
        result = polling_request(*args, **kwargs)
        phase['proven'] = True
        return result
    monkeypatch.setattr(rig.supervisor, 'request', track_polling)
    rollback, flip = forward._rollback, forward._flip
    def track_rollback(*args, **kwargs):
        rollback_calls.append(args[2]['id'])
        return rollback(*args, **kwargs)
    def track_flip(*args, **kwargs):
        phase['flip'] = True
        result = flip(*args, **kwargs)
        phase['returned'] = True
        if caller == 'promote_rollback' and kwargs.get('operation', 'promote') == 'promote':
            rig.alive.pop(args[2]['pid'])
            raise RuntimeError('pointer-committed B died before rollback')
        return result
    monkeypatch.setattr(forward, '_rollback', track_rollback)
    monkeypatch.setattr(forward, '_flip', track_flip)
    def fail_once():
        if not fired:
            fired.append(fault)
            raise OSError('post-pointer activation ' + fault)
    if fault == 'pre_flip_clock':
        now = forward._now
        def broken_pre_flip_clock():
            if phase['proven']:
                fail_once()
            return now()
        monkeypatch.setattr(forward, '_now', broken_pre_flip_clock)
    elif fault == 'orphan_cleanup':
        discard = forward._discard_reservation
        def broken_discard(*args, **kwargs):
            fail_once()
            return discard(*args, **kwargs)
        monkeypatch.setattr(forward, '_discard_reservation', broken_discard)
    elif fault == 'inventory_finish':
        inventory, finish = forward.require_forward_inventory, forward._finish
        def broken_inventory(*args, **kwargs):
            if not fired:
                fired.append(fault)
                raise RuntimeError('inventory qualification unavailable')
            return inventory(*args, **kwargs)
        finish_errors = []
        def broken_blocked_finish(*args, **kwargs):
            if not finish_errors:
                finish_errors.append(True)
                raise OSError('blocked inventory receipt unavailable')
            return finish(*args, **kwargs)
        monkeypatch.setattr(forward, 'require_forward_inventory', broken_inventory)
        monkeypatch.setattr(forward, '_finish', broken_blocked_finish)
    elif fault == 'activation_sync':
        sync = releases._sync_dir
        def broken_sync(path):
            if releases.read_pointer(rig.home / 'current') == expected:
                fail_once()
            return sync(path)
        monkeypatch.setattr(releases, '_sync_dir', broken_sync)
    elif fault in {'activation_verify', 'activation_archive', 'activation_unknown'}:
        name = '_finish_txn' if fault == 'activation_archive' else '_verify_transaction'
        original = getattr(releases, name)
        def broken_transaction(*args, **kwargs):
            assert releases.read_pointer(rig.home / 'current') == expected
            fail_once()
            return original(*args, **kwargs)
        monkeypatch.setattr(releases, name, broken_transaction)
        if fault == 'activation_unknown':
            read_pointer = forward.read_pointer
            def unreadable_pointer(path):
                if fired and phase['flip'] and not phase['returned']:
                    raise OSError('activation pointer readback unavailable')
                return read_pointer(path)
            monkeypatch.setattr(forward, 'read_pointer', unreadable_pointer)
    elif fault in {'commit_save', 'finish_save'}:
        atomic = forward._atomic_json
        def broken_atomic(path, record):
            if (path.name == 'forward-update.json' and record.get('pointer_commit')
                    and (fault == 'commit_save' or record.get('outcome') in {'success', 'rolled_back'})):
                fail_once()
            return atomic(path, record)
        monkeypatch.setattr(forward, '_atomic_json', broken_atomic)
    elif fault in {'pointer_readback', 'final_pointer_readback', 'pointer_inspection'}:
        read_pointer = forward.read_pointer
        def broken_readback(path):
            value = read_pointer(path)
            if value == expected and (phase['flip'] or fault == 'pointer_inspection'):
                phase['reads'] += 1
                if fault != 'final_pointer_readback' or phase['reads'] == 2:
                    fail_once()
            return value
        monkeypatch.setattr(forward, 'read_pointer', broken_readback)
    elif fault in {'owner_readback', 'cleanup'}:
        cleanup = forward.cleanup_exited
        def completed_cleanup(*args, **kwargs):
            if phase['flip']:
                if fault == 'cleanup':
                    fail_once()
                phase['bookkeeping'] = True
            return cleanup(*args, **kwargs)
        monkeypatch.setattr(forward, 'cleanup_exited', completed_cleanup)
        if fault == 'owner_readback':
            lease = forward._lease
            def broken_lease(db):
                if phase['bookkeeping']:
                    fail_once()
                return lease(db)
            monkeypatch.setattr(forward, '_lease', broken_lease)
    elif fault == 'login':
        boot = rig.supervisor.boot_active
        def broken_boot(row, active):
            if active and row['release_sha'] == expected.name:
                fail_once()
            return boot(row, active)
        monkeypatch.setattr(rig.supervisor, 'boot_active', broken_boot)
    elif fault == 'polling_io':
        request = rig.supervisor.request
        def broken_request(*args, **kwargs):
            fail_once()
            return request(*args, **kwargs)
        monkeypatch.setattr(rig.supervisor, 'request', broken_request)
    elif fault == 'promotion_clock':
        now = forward._now
        def broken_clock():
            if phase['returned']:
                fail_once()
            return now()
        monkeypatch.setattr(forward, '_now', broken_clock)
    else:
        if fault == 'receipt':
            from hermes_cli import update_receipt
            module, name = update_receipt, 'record_forward_generation'
        else:
            module, name = forward, '_archive' if fault == 'archive' else '_observe_rollback_reply'
        original = getattr(module, name)
        def broken_finalization(*args, **kwargs):
            fail_once()
            return original(*args, **kwargs)
        monkeypatch.setattr(module, name, broken_finalization)
    holder_id = rig.db.leases()[0]['generation_id'] if caller not in {'promote', 'promote_rollback'} else None
    result = (promote(rig) if caller in {'promote', 'promote_rollback'} else
              forward.recover_forward(rig.home, supervisor=rig.supervisor))
    assert fired == [fault], 'fault must run at the selected real activation boundary'
    expected_rollbacks = [result['new_id']] if caller == 'promote_rollback' else []
    assert rollback_calls == expected_rollbacks, result
    assert result['alert'], result
    expected_outcome = ('rolled_back' if rollback_owner else 'success') if fault in {'login', 'cleanup'} else 'blocked'
    assert result['outcome'] == expected_outcome, result
    holder = forward._row(rig.db, rig.db.leases()[0]['generation_id'])
    assert holder['id'] == (holder_id or (result.get('rollback_generation') or {}).get('id') or result['new_id']) and forward._live(holder)
    if fault != 'activation_unknown':
        assert result['pointer_commit']['generation_id'] == holder['id']
    assert holder['id'] in rig.supervisor.polled
    assert rig.db.leases()[0]['epoch'] == (3 if rollback_owner else 2)
    assert len(rig.db.generations()) == (3 if rollback_owner or fault == 'orphan_cleanup' else 2)
    assert releases.read_pointer(rig.home / 'current') == expected
    if not rollback_owner:
        assert not (rig.home / 'forward-update-bad.json').exists()
    if fault == 'activation_unknown':
        phase['returned'] = True  # Permit readback on the next recovery attempt.
    repaired = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert repaired['outcome'] == ('rolled_back' if rollback_owner else 'success'), repaired
    assert rig.db.leases()[0]['generation_id'] == holder['id']
    assert rig.db.leases()[0]['epoch'] == (3 if rollback_owner else 2)
    assert rollback_calls == expected_rollbacks
    assert not (rig.home / 'forward-update.json').exists()


@pytest.mark.parametrize('caller,health', [
    ('recover', health) for health in ('dies', 'wedged', 'unhealthy', 'unknown')
] + [('promote', health) for health in ('dies', 'wedged')])
def test_review10_committed_holder_health_failure_still_recovers(rig, monkeypatch, caller, health):
    cleanup = forward.cleanup_exited
    def fail_after_flip(*args, **kwargs):
        if releases.read_pointer(rig.home / 'current') == rig.b:
            raise OSError('post-flip bookkeeping error')
        return cleanup(*args, **kwargs)
    if caller == 'promote':
        flip = forward._flip
        def fail_after_activation(*args, **kwargs):
            result = flip(*args, **kwargs)
            if kwargs.get('operation', 'promote') == 'promote':
                rig.supervisor.mode = health
                if health == 'dies':
                    rig.alive.pop(args[2]['pid'])
                raise OSError('post-pointer activation failure with new health fault')
            return result
        monkeypatch.setattr(forward, '_flip', fail_after_activation)
    else:
        monkeypatch.setattr(forward, 'cleanup_exited', fail_after_flip)
    result = promote(rig)
    monkeypatch.setattr(forward, 'cleanup_exited', cleanup)
    if caller == 'recover':
        assert result['outcome'] == 'success' and result['bookkeeping_pending'], result
        rig.supervisor.mode = 'blocked' if health == 'unknown' else health
        result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == ('blocked' if health == 'unknown' else 'rolled_back'), result
    holder = forward._row(rig.db, rig.db.leases()[0]['generation_id'])
    assert forward._live(holder)
    assert holder['release_sha'] == (rig.b.name if health == 'unknown' else rig.a.name)
    assert releases.read_pointer(rig.home / 'current') == (rig.b if health == 'unknown' else rig.a)
    assert len([row for row in rig.db.generations() if row['state'] == 'serving' and forward._live(row)]) == 1
    if health != 'unknown':
        assert holder['id'] != rig.old.id
        assert rig.db.leases()[0]['epoch'] == 3
        assert result['rollback']['commit_to_reply_upper_bound_seconds'] <= 60


