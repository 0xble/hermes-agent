"""Claimed-label respawns terminate before the legacy singleton startup guards."""
import os
import importlib.util
import sys
import signal
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from gateway.config import GatewayConfig
from gateway.generation import GenerationCoordinator, GenerationIdentity


@pytest.mark.asyncio
async def test_claimed_respawn_exits_zero_before_successor_pid_guard(tmp_path, monkeypatch):
    from gateway import run

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_LAUNCHD_LABEL', 'claimed')
    monkeypatch.setenv('HERMES_RELEASE_SHA', 'release')
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: 123)
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture')
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='release', label='claimed', pid=os.getpid() + 1)
    successor = GenerationIdentity.create(release_sha='successor', label='successor', pid=os.getpid() + 2)
    db.register(old, state='draining')
    db.register(successor, state='serving')
    db.acquire_lease('active_generation', successor.id)
    (tmp_path / 'gateway.pid').write_text(str(successor.pid), encoding='utf-8')
    # No usable host record, while the successor already owns gateway.pid.
    host = AsyncMock(return_value=None)
    pid = Mock(return_value=successor.pid)
    resources = Mock(side_effect=AssertionError('respawn touched singleton resources'))
    monkeypatch.setattr(run, '_host_attach_or_none', host)
    monkeypatch.setattr('gateway.status.get_running_pid', pid)
    monkeypatch.setattr(run, '_start_gateway_claim_pid_file', resources)
    claims = Mock(wraps=GenerationCoordinator.claim_process)
    def claim(self, *args, **kwargs):
        return claims(self, *args, **kwargs)
    monkeypatch.setattr(GenerationCoordinator, 'claim_process', claim)
    config = GatewayConfig.from_dict({'gateway': {'forward_only_handover': {'enabled': True}}})
    with pytest.raises(SystemExit) as exit_info:
        await run.start_gateway(config)
    assert exit_info.value.code == 0
    assert claims.call_count == 1
    host.assert_not_awaited()
    pid.assert_not_called()
    resources.assert_not_called()


@pytest.mark.asyncio
async def test_flag_off_respawn_keeps_legacy_duplicate_pid_guard(tmp_path, monkeypatch):
    from gateway import run

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_LAUNCHD_LABEL', 'claimed')
    claim = Mock(side_effect=AssertionError('flag-off claim'))
    monkeypatch.setattr('gateway.run_generation.claim_active_generation', claim)
    monkeypatch.setattr(run, '_host_attach_or_none', AsyncMock(return_value=None))
    monkeypatch.setattr('gateway.status.get_running_pid', lambda: os.getpid() + 1)
    monkeypatch.setattr(run, '_start_gateway_replace_existing_instance', AsyncMock(return_value=False))
    assert await run.start_gateway(GatewayConfig()) is False
    claim.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_overlap_keeps_singleton_guard_before_registration(tmp_path, monkeypatch):
    from gateway import run

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_LAUNCHD_LABEL', 'ai.hermes.gateway')
    db = GenerationCoordinator(tmp_path)
    monkeypatch.setattr(run, '_host_attach_or_none', AsyncMock(return_value=None))
    monkeypatch.setattr('gateway.status.get_running_pid', lambda: os.getpid() + 1)
    monkeypatch.setattr(run, '_start_gateway_replace_existing_instance', AsyncMock(return_value=False))
    config = GatewayConfig.from_dict({'gateway': {
        'overlap_handover': {'enabled': True},
        'forward_only_handover': {'enabled': False},
    }})
    assert await run.start_gateway(config) is False
    assert db.generations() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('history', ['clean_exit', 'several_exits', 'previous_release'])
async def test_legacy_overlap_start_registers_fresh_identity(tmp_path, monkeypatch, history):
    from gateway import run_generation

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_LAUNCHD_LABEL', 'ai.hermes.gateway')
    monkeypatch.setenv('HERMES_RELEASE_SHA', 'new-release')
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: 123)
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture')
    # Exercise real registration, lease acquisition and clean shutdown, without
    # connecting adapters or opening a control socket.
    monkeypatch.setattr(run_generation.ActiveGeneration, 'start', AsyncMock())
    config = GatewayConfig.from_dict({'gateway': {
        'overlap_handover': {'enabled': True},
        'forward_only_handover': {'enabled': False},
    }})
    if history == 'several_exits':
        spec = importlib.util.spec_from_file_location('respawn_previous_generation',
            Path(__file__).parent / 'fixtures/generation_738c502c.py')
        previous = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, previous)
        spec.loader.exec_module(previous)
        legacy = previous.GenerationCoordinator(tmp_path)
        for _ in range(3):
            legacy.register(previous.GenerationIdentity.create(
                release_sha='new-release', label='ai.hermes.gateway', boot_id='fixture'), state='exited')
    db = GenerationCoordinator(tmp_path)
    if history == 'clean_exit':
        old = await run_generation.start_active_generation(config)
        await old.close()
    elif history == 'previous_release':
        db.register(GenerationIdentity.create(
            release_sha='old-release', label='ai.hermes.gateway'), state='exited')
    before = {row['id']: row for row in db.generations()}
    active = await run_generation.start_active_generation(config)
    try:
        assert active.identity.id not in before
        assert active.identity.release_sha == 'new-release'
        rows = {row['id']: row for row in db.generations()}
        assert {key: rows[key] for key in before} == before
        assert rows[active.identity.id]['state'] == 'serving'
        assert db.leases()[0]['generation_id'] == active.identity.id
    finally:
        await active.close()


@pytest.mark.platforms("macos")
@pytest.mark.live_system_guard_bypass
@pytest.mark.asyncio
async def test_legacy_overlap_sigkill_respawn_takes_same_label_lease(tmp_path, monkeypatch):
    from gateway import run_generation
    from gateway.status import _get_process_start_time

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_LAUNCHD_LABEL', 'ai.hermes.gateway')
    monkeypatch.setenv('HERMES_RELEASE_SHA', 'release')
    monkeypatch.setenv('HERMES_GENERATION_SCOPE', 'same-bootstrap')
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture')
    monkeypatch.setattr(run_generation.ActiveGeneration, 'start', AsyncMock())
    config = GatewayConfig.from_dict({'gateway': {'overlap_handover': {'enabled': True}}})
    db = GenerationCoordinator(tmp_path)
    child = subprocess.Popen([sys.executable, '-c',
        "import sys; print('ready', flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        started = _get_process_start_time(child.pid)
        assert started is not None
        old = GenerationIdentity.create(release_sha='release', label='ai.hermes.gateway',
            pid=child.pid, start_fingerprint=f'{child.pid}:{started}')
        db.register(old, state='serving')
        epoch = db.acquire_lease('active_generation', old.id)
        reservation = db.reserve_generation(release_sha='next', label='standby', boot_id='fixture')
        reserved_before = next(row for row in db.generations() if row['id'] == reservation.id)
        os.kill(child.pid, signal.SIGKILL)  # windows-footgun: ok -- macos_only, real SIGKILL is the contract
        child.wait(timeout=5)
        assert next(row for row in db.generations() if row['id'] == old.id)['state'] == 'serving'
        active = await run_generation.start_active_generation(config)
        try:
            assert active.identity.label == old.label and active.identity.pid != old.pid
            assert active.identity.id != old.id
            rows = {row['id']: row for row in db.generations()}
            assert (rows[old.id]['state'], rows[old.id]['verdict'], rows[old.id]['verdict_evidence']) == ('exited', 'failed', 'dead')
            assert rows[active.identity.id]['state'] == 'serving'
            assert rows[reservation.id] == reserved_before
            assert db.leases() == [{'resource': 'active_generation',
                'generation_id': active.identity.id, 'epoch': epoch + 1, 'state': 'active'}]
        finally:
            await active.close()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()


@pytest.mark.parametrize('holder', ['alive', 'unknown_start', 'unclaimed'])
def test_legacy_same_label_refuses_alive_or_unknown_holder(tmp_path, monkeypatch, holder):
    from gateway.run_generation import _claim_legacy_process_generation
    from gateway.status import _get_process_start_time

    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture')
    db = GenerationCoordinator(tmp_path)
    if holder == 'unclaimed':
        db.reserve_generation(release_sha='release', label='service')
    else:
        started = _get_process_start_time(os.getpid())
        assert started is not None
        old = GenerationIdentity.create(release_sha='release', label='service',
            start_fingerprint=f'{os.getpid()}:{started}')
        db.register(old, state='serving')
        db.acquire_lease('active_generation', old.id)
        if holder == 'unknown_start':
            monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: None)
    before, leases = db.generations(), db.leases()
    process = GenerationIdentity.create(release_sha='release', label='service')
    with pytest.raises(RuntimeError, match='same-label.*alive or identity unknown'):
        _claim_legacy_process_generation(db, process)
    assert db.generations() == before
    assert db.leases() == leases
