"""Frozen SQL probes cover additive schema compatibility, without Git history.

After forward-only has run, 738c502c must use the legacy key OFF. Before that
boundary, its legacy overlap writer can still register same-label respawns.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity

FIXTURES = Path(__file__).parent / 'fixtures'
SOURCE_BLOBS = {
    'generation': '0c877b992d8351df3eb614d4c1e5ebfca391e84b',
    'owned_admission': '93d29cfa792c1183afee1c61ac97f3dfc47677ab',
}


def frozen(name):
    path = FIXTURES / f'{name}_738c502c.py'
    header, blob_header, payload = path.read_bytes().split(b'\n', 2)
    assert header == f'# Frozen from 738c502c: gateway/{name}.py'.encode()
    blob = hashlib.sha1(b'blob ' + str(len(payload)).encode() + b'\0' + payload).hexdigest()
    assert blob == SOURCE_BLOBS[name]
    assert blob_header == f'# Git blob: {blob}'.encode()
    spec = importlib.util.spec_from_file_location(f'frozen_{name}', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('name', SOURCE_BLOBS)
def test_frozen_previous_release_matches_source_blob(name):
    frozen(name)


@pytest.mark.parametrize('retirement', ['enqueue', 'hold_dead_owner'])
def test_previous_release_retirement_admits_event(tmp_path, monkeypatch, retirement):
    previous = frozen('owned_admission')
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='old', label='old', boot_id='dead-boot')
    new = GenerationIdentity.create(release_sha='new', label='new', boot_id='current-boot')
    db.register(old, state='draining')
    db.register(new, state='serving')
    epoch = db.acquire_lease('active_generation', new.id)
    # Run the exact previous admission implementation, using its real SQLite transaction.
    legacy = previous.OwnedAdmissionMixin()
    legacy.connect = db.connect
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'current-boot')
    assert legacy.claim_session(str(tmp_path), 'telegram', 'chat', old.id, 1, outstanding_work=1)
    if retirement == 'hold_dead_owner':
        legacy.hold_dead_owner(old.id)
    row, admitted = legacy.enqueue(str(tmp_path), 'telegram', 'chat', 'event', 'message',
                                  json.dumps({'version': 1, 'authorized': True, 'sender': '1'}).encode(),
                                  b'{"text":"new"}', new.id, epoch)
    assert admitted and row['owner_id'] == new.id
    owner = next(row for row in db.generations() if row['id'] == old.id)
    assert (owner['state'], owner['verdict']) == ('exited', 'failed')
    assert db.leases()[0]['generation_id'] == new.id


@pytest.mark.parametrize('state', ['standby', 'serving', 'draining', 'exited'])
def test_previous_ready_heartbeat_preserves_runtime_authority(tmp_path, state):
    previous = frozen('generation')
    db = GenerationCoordinator(tmp_path)
    # Fresh N-1 process opens the existing DB through its actual initializer.
    legacy = previous.GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha='previous', label='rollback', pid=os.getpid(), boot_id='fixture')
    legacy.register(identity)
    if state in ('serving', 'draining'):
        epoch = db.acquire_lease('active_generation', identity.id)
        db.transition_state(identity.id, 'standby', 'serving')
        successor = GenerationIdentity.create(release_sha='new', label='successor', boot_id='fixture')
        db.register(successor)
        db.request_transfer(identity.id, successor.id, epoch, set())
        db.commit_transfer(identity.id, successor.id, epoch)
        target = successor.id if state == 'serving' else identity.id
    else:
        target = identity.id
        if state == 'exited':
            db.heartbeat(target, state='exited')
    before = next(row for row in db.generations() if row['id'] == target)
    leases = db.leases()
    # Exact heartbeat used by 738c502c run_generation.py's ready publication.
    legacy.heartbeat(target, state='ready')
    after = next(row for row in db.generations() if row['id'] == target)
    assert after['state'] == state
    assert after['legacy_ready_at'] == after['heartbeat_at'] >= before['heartbeat_at']
    assert db.leases() == leases
    if state in ('serving', 'draining', 'exited'):
        with legacy.connect() as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE generations SET state='standby' WHERE id=?", (target,))


def test_previous_created_database_upgrades_and_accepts_previous_writes(tmp_path):
    previous = frozen('generation')
    legacy = previous.GenerationCoordinator(tmp_path)
    identity = previous.GenerationIdentity.create(release_sha='previous', label='previous', boot_id='fixture')
    legacy.register(identity)
    epoch = legacy.acquire_lease('active_generation', identity.id)
    legacy.heartbeat(identity.id, state='ready')

    current = GenerationCoordinator(tmp_path)
    assert next(row for row in current.generations() if row['id'] == identity.id)['state'] == 'serving'
    leases = current.leases()
    assert leases[0]['epoch'] == epoch
    # Reopening and writing use the frozen release's initializer and SQL.
    legacy = previous.GenerationCoordinator(tmp_path)
    standby = previous.GenerationIdentity.create(release_sha='previous', label='standby', boot_id='fixture')
    legacy.register(standby)
    legacy.heartbeat(standby.id, state='ready')
    legacy.heartbeat(identity.id, state='ready')
    current = GenerationCoordinator(tmp_path)
    rows = {row['id']: row for row in current.generations()}
    assert rows[identity.id]['state'] == 'serving'
    assert rows[standby.id]['state'] == 'standby'
    assert rows[identity.id]['legacy_ready_at'] is not None
    assert rows[standby.id]['legacy_ready_at'] is not None
    assert current.leases() == leases


def test_previous_database_and_writer_with_legacy_key_off(tmp_path):
    from gateway.config import GatewayConfig

    previous = frozen('generation')
    legacy = previous.GenerationCoordinator(tmp_path)
    history = previous.GenerationIdentity.create(release_sha='previous', label='service', boot_id='fixture')
    legacy.register(history, state='exited')
    current = GenerationCoordinator(tmp_path)
    config = GatewayConfig.from_dict({'gateway': {
        'forward_only_handover': {'enabled': True},
        'overlap_handover': {'enabled': False},
    }})
    # 738c502c sees only the legacy key, so its overlap path stays disabled.
    assert not previous.overlap_handover_enabled(config)
    assert not previous.overlap_handover_enabled({'gateway': {
        'forward_only_handover': {'enabled': True}, 'overlap_handover': {'enabled': False}}})
    legacy = previous.GenerationCoordinator(tmp_path)
    legacy.heartbeat(history.id)
    rows = current.generations()
    assert len(rows) == 1 and rows[0]['id'] == history.id and rows[0]['state'] == 'exited'
    assert current.leases() == []


@pytest.mark.parametrize('entry', ['claim', 'reserve', 'register_unclaimed'])
@pytest.mark.parametrize('death', ['fingerprint', 'absent', 'boot_changed'])
def test_legacy_duplicates_settle_only_at_forward_write(tmp_path, entry, death, monkeypatch):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'current-boot')
    from gateway.generation import _boot_id
    from gateway.generation_schema import layout_is_current
    from gateway.run_generation import _claim_legacy_process_generation
    from gateway.status import _get_process_start_time, _pid_exists

    assert _get_process_start_time(os.getpid()) is not None
    previous = frozen('generation')
    legacy = previous.GenerationCoordinator(tmp_path)
    crashed_pid = 999999999 if death == 'absent' else os.getpid()
    assert death != 'absent' or not _pid_exists(crashed_pid)
    crashed_boot = f'{_boot_id()}:previous' if death == 'boot_changed' else _boot_id()
    labels = {'ai.hermes.gateway': 'serving', 'ai.hermes.gateway-b': 'ready',
              'legacy-start': 'serving'}
    old = {}
    for label, state in labels.items():
        old[label] = []
        for index in range(2):
            row = previous.GenerationIdentity.create(release_sha='previous', label=label,
                pid=crashed_pid, boot_id=crashed_boot, started_at=index + 1,
                start_fingerprint=f'{crashed_pid}:crashed-{index}')
            legacy.register(row, state='serving' if state == 'serving' else 'standby')
            if state == 'ready':
                legacy.heartbeat(row.id, state='ready')
            old[label].append(row)
    holder = old['ai.hermes.gateway'][1]  # Preserve lease authority, not insertion order.
    legacy.acquire_lease('active_generation', holder.id)
    db = GenerationCoordinator(tmp_path)
    leases = db.leases()
    with db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='generations_live_label'").fetchone()

    # The frozen writer can respawn after N opens, with its old crashed row intact.
    respawn = previous.GenerationIdentity.create(release_sha='previous', label=holder.label,
        pid=crashed_pid, boot_id=crashed_boot, start_fingerprint=f'{crashed_pid}:crashed-respawn')
    previous.GenerationCoordinator(tmp_path).register(respawn, state='serving')
    process = GenerationIdentity.create(release_sha='current', label='legacy-start',
        start_fingerprint=f'{os.getpid()}:crashed-legacy-start')
    assert _claim_legacy_process_generation(db, process) == process
    assert all(row['state'] == 'exited' for row in db.generations()
               if row['id'] in {item.id for item in old['legacy-start']})
    with db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='generations_live_label'").fetchone()

    claimant = GenerationIdentity.create(release_sha='current', label=holder.label,
        start_fingerprint=f'{os.getpid()}:{_get_process_start_time(os.getpid())}')
    if entry == 'claim':
        assert db.claim_process(claimant, 'forward-bootstrap').label == holder.label
    elif entry == 'reserve':
        db.reserve_generation(release_sha='current', label='successor')
    else:
        db.register_unclaimed(GenerationIdentity.create(release_sha='current', label='successor'))
    rows = {row['id']: row for row in db.generations()}
    retired = [row.id for row in old[holder.label] if row.id != holder.id] + [respawn.id]
    assert all((rows[key]['state'], rows[key]['verdict'], rows[key]['verdict_evidence']) ==
               ('exited', 'failed', 'boot_changed' if death == 'boot_changed' else 'dead') for key in retired)
    assert rows[holder.id]['state'] == ('exited' if entry == 'claim' else 'serving')
    standby = [rows[row.id] for row in old['ai.hermes.gateway-b']]
    assert all(row['state'] == 'exited' and row['verdict'] == 'failed' for row in standby)
    assert db.leases() == leases
    with db.connect() as conn:
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name='generations_live_label'").fetchone()[0] == (
            "CREATE UNIQUE INDEX generations_live_label ON generations(label) WHERE state <> 'exited'")
        assert layout_is_current(conn, _boot_id())
        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
            conn.execute("INSERT INTO generations "
                "(id,release_sha,label,pid,started_at,boot_id,start_fingerprint,state,heartbeat_at) "
                "SELECT 'collision',release_sha,label,pid,started_at,boot_id,start_fingerprint,state,heartbeat_at "
                "FROM generations WHERE state<>'exited' LIMIT 1")

    # Follow-up 14's legacy start still retires dead rows with the index present.
    assert _claim_legacy_process_generation(db, GenerationIdentity.create(
        release_sha='current', label='legacy-start', start_fingerprint=claimant.start_fingerprint))
    statements = []
    original = GenerationCoordinator.connect
    def traced(self):
        conn = original(self)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(GenerationCoordinator, 'connect', traced)
    for _ in range(3):
        GenerationCoordinator(tmp_path)
    assert not any(sql.lstrip().upper().startswith(('BEGIN IMMEDIATE', 'CREATE ', 'DROP ', 'DELETE ', 'UPDATE ', 'INSERT '))
                   for sql in statements)


@pytest.mark.parametrize('entry', ['claim', 'reserve', 'register_unclaimed'])
@pytest.mark.parametrize('identity', ['alive', 'unknown'])
@pytest.mark.parametrize('label', ['ai.hermes.gateway', 'ai.hermes.gateway-b'])
def test_forward_duplicate_reconciliation_fails_closed(tmp_path, monkeypatch, entry, identity, label):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'current-boot')
    from gateway.generation import _boot_id
    from gateway.status import _get_process_start_time

    assert _get_process_start_time(os.getpid()) is not None
    previous = frozen('generation')
    legacy = previous.GenerationCoordinator(tmp_path)
    rows = []
    for index in range(3):
        row = previous.GenerationIdentity.create(release_sha='previous', label=label,
            pid=os.getpid(), boot_id=_boot_id(), started_at=index + 1,
            start_fingerprint=f'{os.getpid()}:{_get_process_start_time(os.getpid())}' if index == 2
                              else f'{os.getpid()}:crashed-{index}')
        legacy.register(row, state='serving')
        rows.append(row)
    if label == 'ai.hermes.gateway':
        legacy.acquire_lease('active_generation', rows[0].id)
    db = GenerationCoordinator(tmp_path)
    before, leases = db.generations(), db.leases()
    if identity == 'unknown':
        monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: None)
    with pytest.raises(RuntimeError, match=f'{label}.*alive or identity unknown'):
        if entry == 'claim':
            db.claim_process(GenerationIdentity.create(release_sha='current', label=label), 'forward')
        elif entry == 'reserve':
            db.reserve_generation(release_sha='current', label='successor')
        else:
            db.register_unclaimed(GenerationIdentity.create(release_sha='current', label='successor'))
    assert db.generations() == before
    assert db.leases() == leases
    with db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='generations_live_label'").fetchone()
