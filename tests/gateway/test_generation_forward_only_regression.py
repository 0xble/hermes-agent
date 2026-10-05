"""Behavior regressions that fail against the unmodified coordinator API."""
import os
import pytest
from gateway.generation import GenerationCoordinator, GenerationIdentity


@pytest.mark.parametrize('state', ['draining', 'exited'])
def test_old_runtime_cannot_serve_again(tmp_path, state):
    db = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha='r', label='r', boot_id='fixture')
    db.register(identity, state=state)
    with pytest.raises(RuntimeError):
        db.heartbeat(identity.id, state='serving')


def test_lease_acquisition_cannot_implicitly_take_over_dead_owner(tmp_path, monkeypatch):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'current-boot')
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='old', label='old', boot_id='previous-boot')
    new = GenerationIdentity.create(release_sha='new', label='new', boot_id='current-boot')
    db.register(old, state='serving')
    db.register(new)
    epoch = db.acquire_lease('active_generation', old.id)
    with pytest.raises(RuntimeError):
        db.acquire_lease('active_generation', new.id)
    assert db.leases()[0]['epoch'] == epoch


@pytest.mark.asyncio
async def test_promotion_during_standby_publication_starts_committed_successor(tmp_path, monkeypatch):
    """A claimed standby is promotable before its final publication heartbeat."""
    from gateway import run_generation
    from gateway.config import GatewayConfig
    from gateway import run

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_LAUNCHD_LABEL', 'successor')
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture')
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: 123)
    coordinator = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='old', label='old', pid=os.getpid() + 1,
                                    boot_id='fixture', start_fingerprint='old')
    coordinator.register(old, state='serving')
    epoch = coordinator.acquire_lease('active_generation', old.id)
    promoted = []
    started = []

    class Server:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def bind(callback, *, path):
        # Only the socket boundary is replaced. Coordinator writes are real.
        from pathlib import Path
        Path(path).touch()
        return Server()

    original_write = run_generation.write_generation_record

    def publish(path, identity, **kwargs):
        original_write(path, identity, **kwargs)
        if kwargs.get('socket_path') is not None:
            coordinator.request_transfer(old.id, identity.id, epoch, tokens=[])
            promoted.append((identity, coordinator.commit_transfer(old.id, identity.id, epoch)))

    async def start_gateway(config, *, promoted_generation):
        started.append(promoted_generation)
        return True

    monkeypatch.setattr(run_generation.asyncio, 'start_unix_server', bind)
    monkeypatch.setattr(run_generation, 'write_generation_record', publish)
    monkeypatch.setattr(run, 'start_gateway', start_gateway)
    config = GatewayConfig.from_dict({'gateway': {'overlap_handover': {'enabled': True}}})
    assert await run_generation.serve_standby_generation(config)
    assert started == promoted
    successor = next(row for row in coordinator.generations() if row['label'] == 'successor')
    assert successor['state'] == 'serving' and successor['verdict'] is None
    assert coordinator.leases()[0]['epoch'] == epoch + 1
