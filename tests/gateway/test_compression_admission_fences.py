"""Ownership and shutdown fences survive the final compression-lock await."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageEvent
from tests.fakes.runtime_owner import initialize_session_authority
from tests.gateway.test_compression_admission_retry import _cleanup, _owner, _rows


@pytest.mark.asyncio
@pytest.mark.parametrize('fence', ['restart', 'new-epoch'])
async def test_compression_release_rechecks_fences(monkeypatch, tmp_path, fence):
    runner, adapter, owner, source, ref, _key = await _owner(monkeypatch, tmp_path)
    sid, db = ref.session_id, owner.db
    assert db.try_acquire_compression_lock(sid, 'fixture-compressor')
    deferred, checking, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    defer = runner._defer_compression_locked_event
    check = runner._session_has_compression_in_flight
    model = AsyncMock(side_effect=AssertionError('fenced owner called the model'))
    monkeypatch.setattr(runner, '_run_agent', model)

    async def observe(*args, **kwargs):
        result = await defer(*args, **kwargs)
        if result:
            deferred.set()
        return result

    async def held_check(session_key):
        if deferred.is_set():
            checking.set()
            await release.wait()
            return False
        return await check(session_key)

    monkeypatch.setattr(runner, '_defer_compression_locked_event', observe)
    monkeypatch.setattr(runner, '_session_has_compression_in_flight', held_check)
    try:
        await adapter.handle_message(MessageEvent(text='A', source=source, message_id='a'))
        await asyncio.wait_for(checking.wait(), 10)
        old_task = owner.sessions[sid].task
        assert _rows(owner, ref)[0]['status'] == 'started'
        if fence == 'restart':
            runner._restart_requested = True
            runner._draining = True
        else:
            await initialize_session_authority(runner, profile_id='default', instance_id='successor')
        db.release_compression_lock(sid, 'fixture-compressor')
        release.set()
        await asyncio.wait_for(asyncio.shield(old_task), 10)
        assert model.await_count == 0
        assert _rows(owner, ref)[0]['status'] == ('queued' if fence == 'restart' else 'unknown')
        assert not adapter._pending_messages
        assert not any('compression' in reply.lower() for reply in adapter.sent)
    finally:
        release.set()
        db.release_compression_lock(sid, 'fixture-compressor')
        await _cleanup(runner, adapter, owner)
