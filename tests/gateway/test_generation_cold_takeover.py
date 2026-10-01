"""Production cold activation is gated and uses the existing takeover move."""
import os

import pytest

from gateway.config import GatewayConfig
from gateway.generation import GenerationCoordinator, GenerationIdentity
from gateway import run_generation


@pytest.mark.asyncio
@pytest.mark.parametrize('holder', ['clean', 'dead', 'live', 'unknown'])
async def test_cold_start_gate_then_takeover_or_block(tmp_path, monkeypatch, holder):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='old', label='ai.hermes.gateway',
        pid=123, start_fingerprint='123:1', boot_id='boot')
    db.register(old, state='serving')
    db.acquire_lease('active_generation', old.id)
    if holder == 'clean':
        db.release_lease('active_generation', old.id, 1)
        db.heartbeat(old.id, state='exited')
    new = db.reserve_generation(release_sha='new', label='successor', boot_id='boot')
    db.claim_generation(new.id, os.getpid(), 'new', boot_id='boot', scope_nonce='new')
    new = GenerationIdentity(**{key: db.generations()[-1][key] for key in GenerationIdentity.__dataclass_fields__})
    monkeypatch.setattr('gateway.status._pid_exists', lambda pid: holder in {'live', 'unknown'})
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: None if holder == 'unknown' else 1)
    calls = []
    async def gate(config):
        assert db.leases()[0]['generation_id'] == old.id
        from gateway.startup_gate import StartupGateVerdict
        calls.append('gate')
        return StartupGateVerdict('passed', {}, 0.1)
    async def start(self):
        calls.append('start')
    monkeypatch.setattr('gateway.startup_gate.run_startup_gate', gate)
    monkeypatch.setattr(run_generation.ActiveGeneration, 'start', start)
    monkeypatch.setattr(run_generation, '_bootout_retired_generation', lambda label: calls.append('bootout') or True, raising=False)
    config = GatewayConfig(forward_only_handover_enabled=True)
    if holder in {'live', 'unknown'}:
        with pytest.raises(RuntimeError, match='death proof'):
            await run_generation.start_active_generation(config, claimed_generation=(db, new))
        assert db.leases()[0]['generation_id'] == old.id and 'start' not in calls
    else:
        active = await run_generation.start_active_generation(config, claimed_generation=(db, new))
        assert active.epoch == 2 and calls == ['gate', 'bootout', 'start']
        assert db.leases()[0]['generation_id'] == new.id
        with db.connect() as conn:
            assert conn.execute('SELECT kind FROM lease_moves').fetchone()[0] == 'takeover'
        retired = next(row for row in db.generations() if row['id'] == old.id)
        assert retired['verdict'] == (None if holder == 'clean' else 'failed')


@pytest.mark.asyncio
@pytest.mark.parametrize('holder', ['clean', 'dead', 'live', 'unknown'])
async def test_production_start_claims_scope_before_cold_activation(tmp_path, monkeypatch, holder):
    from gateway import run
    original_start = run.start_gateway
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_LAUNCHD_LABEL', 'ai.hermes.gateway')
    monkeypatch.setenv('HERMES_GENERATION_SCOPE', 'fresh-bootstrap')
    monkeypatch.setenv('HERMES_RELEASE_SHA', 'new-release')
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='old', label='ai.hermes.gateway',
        pid=123, start_fingerprint='123:1', boot_id='boot')
    db.register(old, state='serving')
    db.acquire_lease('active_generation', old.id)
    if holder == 'clean':
        db.release_lease('active_generation', old.id, 1)
        db.heartbeat(old.id, state='exited')
    before = db.generations()
    monkeypatch.setattr('gateway.status._pid_exists', lambda pid: holder in {'live', 'unknown'})
    monkeypatch.setattr('gateway.status._get_process_start_time',
        lambda pid: None if pid == 123 and holder == 'unknown' else 1)
    calls = []
    async def gate(config):
        assert db.leases()[0]['generation_id'] == old.id
        row = next(row for row in db.generations() if row['release_sha'] == 'new-release')
        assert row['scope_nonce'] == 'fresh-bootstrap' and row['state'] == 'standby'
        from gateway.startup_gate import StartupGateVerdict
        calls.append('gate')
        return StartupGateVerdict('passed', {}, 0.1)
    async def promoted(config, *, promoted_generation):
        identity, epoch = promoted_generation
        assert identity.release_sha == 'new-release' and epoch == 2
        assert db.leases()[0]['generation_id'] == identity.id
        calls.append('promoted')
        return True
    monkeypatch.setattr('gateway.startup_gate.run_startup_gate', gate)
    monkeypatch.setattr(run, 'start_gateway', promoted)
    monkeypatch.setattr(run_generation, '_bootout_retired_generation',
        lambda label: pytest.fail('the taker must never boot out its own label'))
    config = GatewayConfig(forward_only_handover_enabled=True)
    if holder in {'live', 'unknown'}:
        with pytest.raises(SystemExit) as exc:
            await original_start(config)
        assert exc.value.code == 0 and db.generations() == before and calls == []
    else:
        assert await original_start(config)
        assert calls == ['gate', 'promoted']
        retired = next(row for row in db.generations() if row['id'] == old.id)
        assert retired['state'] == 'exited'
        assert retired['verdict'] == (None if holder == 'clean' else 'failed')
