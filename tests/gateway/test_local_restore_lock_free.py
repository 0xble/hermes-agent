"""Producer restoration keeps the owner loop responsive while validating a live route off-loop.

Current live sessions still revalidate their durable receipt and lineage. Contention on another
turn's SessionStore write must not move that validation back onto the gateway loop.
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_restoring_a_current_live_route_keeps_the_owner_loop_responsive(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from tests.fakes.runtime_owner import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    from gateway import session_local_recovery

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path / 'fixture'), instance_id='owner')
    viewer = AuthorityConnection(authority, object(), {'user_id': 'human', 'profile_id': str(tmp_path / 'fixture')})
    ref = create_local_session(authority, viewer.actor, {'request_id': 'r', 'source': 'cli',
                                                         'cwd': str(tmp_path), 'model': 'm', 'toolsets': []})
    await session_local_recovery.restore_local_session_async(authority, ref.session_id)
    assert ref.session_id in authority.sessions

    held, release = threading.Event(), threading.Event()

    def writer_holding_the_lock():  # another turn's worker inside a state.db write
        with store._lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=writer_holding_the_lock)
    holder.start()
    loop = asyncio.get_running_loop()
    validating = asyncio.Event()
    restore_entry = session_local_recovery._restore_local_entry

    def observe_validation(*args, **kwargs):
        loop.call_soon_threadsafe(validating.set)
        return restore_entry(*args, **kwargs)

    monkeypatch.setattr(session_local_recovery, '_restore_local_entry', observe_validation)
    restoring = None
    try:
        assert await asyncio.to_thread(held.wait, 5)
        restoring = asyncio.create_task(
            session_local_recovery.restore_local_session_async(authority, ref.session_id))
        await asyncio.wait_for(validating.wait(), 3)
        assert not restoring.done(), 'the held store lock must still fence durable route validation'
        release.set()  # only the responsive owner loop can let the validation finish
        restored = await asyncio.wait_for(restoring, 3)
        assert restored.session_id == ref.session_id
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 10)
        if restoring is not None:
            await asyncio.gather(restoring, return_exceptions=True)
        store._db.close()
