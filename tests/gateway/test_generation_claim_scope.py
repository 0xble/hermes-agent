"""A supervisor scope is single-use, including after claimant retirement."""
from dataclasses import replace
import os
import sqlite3

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity
from gateway.run_generation import _claim_process_generation


@pytest.fixture(autouse=True)
def boot(monkeypatch):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    monkeypatch.setenv('HERMES_GENERATION_SCOPE', 'bootstrap')


def process(label, sha='release', boot='boot'):
    return GenerationIdentity.create(release_sha=sha, label=label, boot_id=boot,
                                     start_fingerprint='claimant')


@pytest.mark.parametrize('retired', [False, True])
def test_consumed_scope_exits_without_mutation(tmp_path, monkeypatch, retired):
    db = GenerationCoordinator(tmp_path)
    first = _claim_process_generation(db, process(label=db.service_label()))
    monkeypatch.setattr(db, '_owner_is_dead', lambda row: True)
    if retired:
        assert db.retire_dead_generation(first.id, expected_pid=first.pid,
            expected_start_fingerprint=first.start_fingerprint, evidence='dead')
    before = db.generations()
    with pytest.raises(SystemExit) as exc:
        _claim_process_generation(db, replace(process(label=db.service_label()), pid=os.getpid() + 1))
    assert exc.value.code == 0
    assert db.generations() == before
    assert db.leases() == []
    assert before[0]['scope_nonce'] == 'bootstrap'


@pytest.mark.parametrize('reboot', [False, True])
def test_service_new_scope_replaces_dead_release_in_standby(tmp_path, monkeypatch, reboot):
    db = GenerationCoordinator(tmp_path)
    old = _claim_process_generation(db, process(label=db.service_label()))
    epoch = db.acquire_lease('active_generation', old.id)
    db.transition_state(old.id, 'standby', 'serving')
    if reboot:
        monkeypatch.setattr('gateway.generation._boot_id', lambda: 'reboot')
    else:
        monkeypatch.setenv('HERMES_GENERATION_SCOPE', 'new-bootstrap')
    monkeypatch.setattr(db, '_owner_is_dead', lambda row: True)
    new = _claim_process_generation(db, process(label=db.service_label(), sha='new-release', boot='reboot' if reboot else 'boot'))
    rows = {row['id']: row for row in db.generations()}
    assert new.id != old.id and new.release_sha == 'new-release'
    assert (rows[old.id]['state'], rows[old.id]['verdict'], rows[old.id]['verdict_evidence']) == (
        'exited', 'failed', 'boot_changed' if reboot else 'dead')
    assert rows[new.id]['state'] == 'standby'
    assert db.leases() == [{'resource': 'active_generation', 'generation_id': old.id,
                           'epoch': epoch, 'state': 'active'}]


@pytest.mark.parametrize('dead', [False, True])
def test_nonservice_claimant_never_reserves_itself(tmp_path, monkeypatch, dead):
    db = GenerationCoordinator(tmp_path)
    reserved = db.reserve_generation(label='drainer', release_sha='release')
    first = _claim_process_generation(db, process(label='drainer'))
    assert first.id == reserved.id
    monkeypatch.setenv('HERMES_GENERATION_SCOPE', 'new-bootstrap')
    monkeypatch.setattr(db, '_owner_is_dead', lambda row: dead)
    with pytest.raises(SystemExit) as exc:
        _claim_process_generation(db, process(label='drainer', sha='other'))
    assert exc.value.code == 0 and len(db.generations()) == 1
    assert db.generations()[0]['state'] == ('exited' if dead else 'standby')
    with pytest.raises(SystemExit):
        _claim_process_generation(db, process(label='unreserved'))
    assert len(db.generations()) == 1


def test_live_service_blocks_new_scope_and_release(tmp_path, monkeypatch):
    db = GenerationCoordinator(tmp_path)
    _claim_process_generation(db, process(label=db.service_label()))
    before = db.generations()
    monkeypatch.setenv('HERMES_GENERATION_SCOPE', 'different')
    monkeypatch.setattr(db, '_owner_is_dead', lambda row: False)
    with pytest.raises(SystemExit) as exc:
        _claim_process_generation(db, process(label=db.service_label(), sha='new-release'))
    assert exc.value.code == 0
    assert db.generations() == before


def test_exited_label_can_be_reserved_but_live_collision_is_rejected(tmp_path):
    db = GenerationCoordinator(tmp_path)
    first = db.reserve_generation(label='label', release_sha='r')
    assert db.retire_unclaimed(first.id)
    second = db.reserve_generation(label='label', release_sha='r2')
    assert second.id != first.id
    with pytest.raises(sqlite3.IntegrityError):
        db.reserve_generation(label='label', release_sha='r3')


def test_existing_branch_database_upgrades_without_losing_authority(tmp_path):
    from pathlib import Path
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("branch_db_fixture",
        Path(__file__).parent / "fixtures/generation_738c502c.py")
    previous = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = previous
    spec.loader.exec_module(previous)
    legacy = previous.GenerationCoordinator(tmp_path)
    old = previous.GenerationIdentity.create(release_sha='old', label='ai.hermes.gateway', boot_id='boot')
    legacy.register(old)
    legacy.acquire_lease('active_generation', old.id)
    legacy.heartbeat(old.id, state='ready')
    # Recreate the constraints installed by this branch before the scope amendment.
    with legacy.connect() as conn:
        conn.execute("ALTER TABLE generations ADD COLUMN claim_pending INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE TRIGGER lease_claim_required BEFORE INSERT ON leases "
                     "WHEN NOT EXISTS(SELECT 1 FROM generations WHERE id=NEW.generation_id "
                     "AND claim_pending=0 AND pid>0) "
                     "BEGIN SELECT RAISE(ABORT,'lease requires a claimed live generation'); END")
        conn.execute("CREATE TRIGGER generation_unique_label BEFORE INSERT ON generations "
                     "WHEN EXISTS(SELECT 1 FROM generations WHERE label=NEW.label) "
                     "BEGIN SELECT RAISE(ABORT,'generation label already reserved'); END")
    db = GenerationCoordinator(tmp_path)
    assert db.generations()[0]['id'] == old.id
    assert db.generations()[0]['scope_nonce'] is None
    with db.connect() as conn:
        info = {row['name']: row for row in conn.execute('PRAGMA table_info(generations)')}
        assert not info['pid']['notnull'] and not info['start_fingerprint']['notnull']
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    # The real previous initializer and writer still operate on the upgraded DB.
    previous.GenerationCoordinator(tmp_path).heartbeat(old.id, state='ready')
    assert db.generations()[0]['state'] == 'serving'
