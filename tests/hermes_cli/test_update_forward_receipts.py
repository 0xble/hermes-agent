"""Forward update runtime evidence and existing receipt consumer contracts."""
import json
from pathlib import Path

import pytest

from gateway.update_notifications import final_outcome
from hermes_cli import update_receipt
from hermes_cli.update_cmd_fleet import _receipt_looks_unfinished
from hermes_cli.web_routers.actions import _completed_exit_code


@pytest.fixture
def receipt_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    token = update_receipt._current.set(None)
    monkeypatch.setattr(update_receipt, '_code_identity', lambda **kwargs: {'sha': 'source-sha'})
    yield tmp_path
    update_receipt._current.reset(token)


@pytest.mark.parametrize('outcome', ['success', 'rolled_back'])
def test_review_m3_forward_receipt_carries_verified_fleet(receipt_home, outcome):
    home = receipt_home
    sha = ('a' if outcome == 'rolled_back' else 'b') * 40
    release = home / 'releases' / sha
    release.mkdir(parents=True)
    (release / '.release-ready').write_text(sha, encoding='utf-8')
    (release / '.hermes_build_sha').write_text(sha, encoding='utf-8')
    (home / 'current').symlink_to(release)
    proof = {'generation_id': 'proven-owner', 'release_sha': sha, 'release_root': str(release),
             'pid': 321, 'label': 'fixture-label', 'epoch': 3, 'polling': True, 'healthy': True,
             'tokens': ['hash'], 'poller_started_at': 1}
    forward = {'outcome': outcome, 'new_sha': 'b' * 40, 'new_id': 'failed-owner', 'poller': proof}
    if outcome == 'rolled_back':
        forward['rollback'] = {'poller': proof, 'new_id': proof['generation_id'], 'new_sha': sha}
    update_receipt.begin_update_receipt()
    pending = {'notification_version': 2, 'timestamp': update_receipt._current.get().data['started_at']}
    update_receipt.record_forward_generation(forward)
    update_receipt.finalize_update_receipt('success' if outcome == 'success' else 'partial')
    (home / '.update_process_exit_code').write_text('0', encoding='utf-8')
    receipt = update_receipt.read_latest_receipt()
    assert len(receipt['fleet']) == 1
    row = receipt['fleet'][0]
    assert {key: row[key] for key in ('code_sha', 'pid', 'generation_id', 'label', 'epoch', 'state')} == {
        'code_sha': sha, 'pid': 321, 'generation_id': 'proven-owner', 'label': 'fixture-label', 'epoch': 3, 'state': 'current'}
    success, detail = final_outcome(home, pending)
    assert success is (outcome == 'success')
    if success:
        assert 'running gateway revision was verified' in detail
    else:
        assert 'partial' in detail
    assert 'Runtime state is unknown' not in detail and 'already at revision' not in detail


@pytest.mark.parametrize('forward_outcome,mapped,exit_code,unfinished', [
    ('success', 'success', 0, False), ('rolled_back', 'partial', 1, True),
    ('aborted', 'refused', 1, True), ('refused', 'refused', 1, True), ('blocked', 'failed', 1, True),
])
def test_review_l2_forward_outcomes_use_existing_consumer_vocabulary(
        receipt_home, forward_outcome, mapped, exit_code, unfinished):
    update_receipt.begin_update_receipt()
    pending = {'notification_version': 2, 'timestamp': update_receipt._current.get().data['started_at']}
    update_receipt.record_forward_generation({'outcome': forward_outcome, 'failure': 'fixture reason',
                                             'alert': forward_outcome == 'blocked'})
    update_receipt.finalize_update_receipt(forward_outcome)
    receipt = json.loads((receipt_home / 'logs/update_receipts/latest.json').read_text(encoding='utf-8'))
    assert receipt['outcome'] == mapped
    assert receipt['forward_generation']['outcome'] == forward_outcome
    assert _completed_exit_code(None, None, receipt) == exit_code
    assert _receipt_looks_unfinished(receipt) is unfinished
    (receipt_home / '.update_process_exit_code').write_text('0', encoding='utf-8')
    if mapped != 'success':
        result = final_outcome(receipt_home, pending)
        assert result is not None and result[0] is False


@pytest.mark.parametrize('fails', [False, True])
def test_review2_post_swap_forward_finishes_once_and_discharges_restart(monkeypatch, fails):
    from hermes_cli import update_cmd, gateway_forward_update as forward
    calls = []
    proof = {'outcome': 'success', 'new_sha': 'b' * 40}
    monkeypatch.setattr(forward, 'require_forward_inventory', lambda home: None)
    def verify(home, record):
        if fails:
            raise RuntimeError('poller proof owner changed')
        return {**record, 'verified': True}
    monkeypatch.setattr(forward, 'verify_forward', verify)
    monkeypatch.setattr(update_receipt, 'record_forward_generation', lambda record: calls.append(('record', record['outcome'])))
    monkeypatch.setattr(update_cmd, '_record_update_step', lambda name, ok, detail: calls.append(('step', ok)))
    monkeypatch.setattr(update_cmd, '_clear_fleet_restart_pending_marker', lambda: calls.append(('clear',)))
    monkeypatch.setattr(update_cmd, '_write_gateway_update_exit_code', lambda ok: calls.append(('exit_code', ok)))
    monkeypatch.setattr(update_cmd, '_finalize_receipt', lambda status, msg: calls.append(('final', status)))
    if fails:
        with pytest.raises(SystemExit):
            update_cmd._complete_forward_update(Path('/nonexistent'), proof)
        assert calls == [('record', 'blocked'), ('step', False), ('exit_code', False), ('final', 'failed')]
    else:
        update_cmd._complete_forward_update(Path('/nonexistent'), proof)
        assert calls == [('record', 'success'), ('step', True), ('clear',), ('exit_code', True), ('final', 'success')]
