"""The busy /stop route probes durable state off-loop but mounts authority state on-loop."""
from __future__ import annotations

from threading import get_ident

import pytest

from tests.fakes.runtime_owner import initialize_session_authority
from tests.gateway.test_superseded_queued_rows import _queue_rows, _rows, _setup


@pytest.mark.asyncio
async def test_stop_route_mount_keeps_authority_mutation_on_gateway_loop(monkeypatch):
    """The real /stop route mounts a cold owner on-loop before cancelling its queued rows."""
    from gateway import session_route_recovery

    runner, adapter = _setup(monkeypatch)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    route, owner = await _queue_rows(runner, authority, source, 'A', 'B')
    authority.sessions.pop(owner)

    loop_thread = get_ident()
    probe_threads: list[int] = []
    plan_threads: list[int] = []
    mount_threads: list[int] = []
    real_probe = runner._hm_mounted_owner_probe
    real_plan = runner._hm_mounted_owner_mount_plan
    real_restore = session_route_recovery.restore_routed_session

    def probe(*args):
        probe_threads.append(get_ident())
        return real_probe(*args)

    def restore_routed_session(*args, **kwargs):
        mount_threads.append(get_ident())
        return real_restore(*args, **kwargs)

    def mount_plan(*args):
        plan_threads.append(get_ident())
        return real_plan(*args)

    monkeypatch.setattr(runner, '_hm_mounted_owner_probe', probe)
    monkeypatch.setattr(runner, '_hm_mounted_owner_mount_plan', mount_plan)
    monkeypatch.setattr(session_route_recovery, 'restore_routed_session', restore_routed_session)
    try:
        await runner._hm_stop_owner_turn(route)
        assert probe_threads and loop_thread not in probe_threads
        assert plan_threads and loop_thread not in plan_threads
        assert mount_threads == [loop_thread]
        assert owner in authority.sessions
        assert _rows(authority, owner) == [('A', 'terminal', 'cancelled'), ('B', 'terminal', 'cancelled')]
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_stop_route_mount_plan_failure_still_fences_and_cancels(monkeypatch):
    """A cold storage read failure must not skip the route's already committed cancellation."""
    from gateway import session_route_recovery

    runner, adapter = _setup(monkeypatch)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    route, owner = await _queue_rows(runner, authority, source, 'A', 'B')
    authority.sessions.pop(owner)
    before = authority.route_epoch(route)

    def fail_plan(*args):
        raise RuntimeError('database temporarily unavailable')

    monkeypatch.setattr(session_route_recovery, '_mount_plan', fail_plan)
    await runner._hm_stop_owner_turn(route)

    assert authority.route_epoch(route) == before + 1
    assert _rows(authority, owner) == [('A', 'terminal', 'cancelled'), ('B', 'terminal', 'cancelled')]
    authority.db.close()


@pytest.mark.asyncio
async def test_stop_route_mount_plan_failure_mounts_repointed_owner_detached(monkeypatch):
    """A lane repointed during a failed cold read still mounts the observed old owner detached."""
    import copy

    from gateway import session_route_recovery

    runner, adapter = _setup(monkeypatch)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    route, owner = await _queue_rows(runner, authority, source, 'A', 'B')
    authority.sessions.pop(owner)
    real_probe = runner._hm_mounted_owner_probe

    def probe(*args):
        result = real_probe(*args)
        store = runner.session_store
        with store._lock:
            moved = copy.copy(store._entries[route])
            moved.session_id = 'newer-conversation'
            moved._route_version = getattr(moved, '_route_version', 0) + 1
            store._entries[route] = moved
        return result

    def fail_plan(*args):
        raise RuntimeError('database temporarily unavailable')

    monkeypatch.setattr(runner, '_hm_mounted_owner_probe', probe)
    monkeypatch.setattr(session_route_recovery, '_mount_plan', fail_plan)
    await runner._hm_stop_owner_turn(route)

    assert owner in authority.sessions
    assert authority.sessions[owner].detached
    assert _rows(authority, owner) == [('A', 'terminal', 'cancelled'), ('B', 'terminal', 'cancelled')]
    authority.db.close()


@pytest.mark.asyncio
async def test_stop_route_unmountable_owner_fences_without_on_loop_cold_mount(monkeypatch):
    """With no usable plan the owner is not settled, so authorize never cold-mounts it on the loop."""
    from gateway import session_route_recovery

    runner, adapter = _setup(monkeypatch)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    route, owner = await _queue_rows(runner, authority, source, 'A', 'B')
    authority.sessions.pop(owner)
    before = authority.route_epoch(route)
    loop_thread = get_ident()
    cold_mounts: list[int] = []
    real_mount_plan = session_route_recovery._mount_plan

    def fail_plan(*args):
        if get_ident() == loop_thread:
            cold_mounts.append(get_ident())
        raise RuntimeError('database temporarily unavailable')

    real_probe = runner._hm_mounted_owner_probe

    def probe(*args):
        sid, entry = real_probe(*args)
        if entry is not None:
            import copy
            entry = copy.copy(entry)
            entry.origin = None
        return sid, entry

    monkeypatch.setattr(runner, '_hm_mounted_owner_probe', probe)
    monkeypatch.setattr(session_route_recovery, '_mount_plan', fail_plan)
    await runner._hm_stop_owner_turn(route)

    assert real_mount_plan is not fail_plan
    assert cold_mounts == []
    assert owner not in authority.sessions
    assert authority.route_epoch(route) > before
    assert _rows(authority, owner) == [('A', 'queued', None), ('B', 'queued', None)]
    authority.db.close()


@pytest.mark.asyncio
async def test_stop_route_probe_failure_still_fences(monkeypatch):
    """A durable owner probe failure must not leave the route unfenced."""
    runner, adapter = _setup(monkeypatch)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    route, owner = await _queue_rows(runner, authority, source, 'A', 'B')
    before = authority.route_epoch(route)

    def fail_probe(*args):
        raise RuntimeError('database temporarily unavailable')

    monkeypatch.setattr(runner, '_hm_mounted_owner_probe', fail_probe)
    await runner._hm_stop_owner_turn(route)

    assert authority.route_epoch(route) == before + 1
    assert _rows(authority, owner) == [('A', 'queued', None), ('B', 'queued', None)]
    authority.db.close()
