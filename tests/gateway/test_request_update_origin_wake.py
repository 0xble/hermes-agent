"""Exercise the native request marker through terminal durable session admission."""
import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_context import clear_session_vars, set_session_vars
from gateway.session_envelope import restore_native
from gateway.update_launcher import launch_native_update
from gateway.update_notifications import read_pending
from hermes_state_runtime import list_session_admissions
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.fakes.runtime_owner import initialize_session_authority
from tests.gateway.update_fixtures import finalize_update
from tests.waiting import async_wait_until


@pytest_asyncio.fixture
async def origin(tmp_path, monkeypatch):
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False))
    monkeypatch.setattr(adapter, 'gateway_runner', runner)
    runner.adapters[Platform.DISCORD] = adapter
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='update-wake')
    home = Path(authority.db.db_path).parent
    monkeypatch.setattr('gateway.run._hermes_home', home)
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', user_id='42', chat_type='dm', thread_id='77')
    key = runner._session_key_for_source(source)
    send = AsyncMock(return_value=SendResult(success=True, message_id='sent'))
    monkeypatch.setattr(adapter, 'send', send)
    turn = AsyncMock(return_value='seed reply')
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    await adapter.handle_message(MessageEvent(text='seed', source=source, message_id='seed'))
    await async_wait_until(lambda: not adapter._background_tasks, timeout=10, what='seed turn')
    sid = runner.session_store.peek_session_id(key)
    assert sid
    turn.reset_mock()
    send.reset_mock()
    tokens = set_session_vars(platform='discord', chat_id='42', user_id='42', chat_type='dm',
                              thread_id='77', session_key=key, session_id=sid, profile='default')
    try:
        assert launch_native_update(home=home, hermes_cmd=['hermes'], spawn=Mock(), pending={
            'source': 'request_update', 'platform': 'discord', 'chat_id': '42',
            'user_id': '42', 'chat_type': 'dm', 'thread_id': '77', 'profile': 'default',
            'session_key': key, 'reason': 'Verify candidate', 'timestamp': datetime.now(UTC).isoformat(),
        })['started']
    finally:
        clear_session_vars(tokens)

    try:
        yield runner, adapter, authority, sid, key, turn, send, home
    finally:
        from gateway.session_runtime_workers import mutation_tasks
        tasks = list(adapter._background_tasks) + [live.task for live in authority.sessions.values() if live.task]
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        while pending := mutation_tasks(authority):
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)
        authority.db.close()
        runner.session_store.close_all_db_handles()


def wakes(authority, sid):
    return [row for row in list_session_admissions(authority.db, session_id=sid, pending_only=False)
            if row['principal_id'].startswith('automation:')]


def runtime_status(tmp_path, monkeypatch, *, sha='a' * 40, state='running'):
    from gateway.status import flush_runtime_status, write_runtime_status

    monkeypatch.setattr('gateway.status.live_gateway_pid_for_home', lambda home: os.getpid())
    monkeypatch.setattr('gateway.status._get_code_identity_fields',
                        lambda: {'code_sha': sha, 'code_version': '0.local-proof'})
    monkeypatch.setattr('gateway.status._get_runtime_status_path', lambda: tmp_path / 'gateway_state.json')
    write_runtime_status(gateway_state=state)
    assert flush_runtime_status()


@pytest.mark.asyncio
async def test_completed_request_update_wakes_original_session(origin, monkeypatch):
    runner, _, authority, sid, _, turn, send, tmp_path = origin
    # Pre-restart wrapper handoff is not a completed updater process.
    (tmp_path / '.update_exit_code').write_text('0', encoding='utf-8')
    assert not await runner._send_update_notification()
    assert not await runner._send_update_notification(timed_out=True)
    assert not wakes(authority, sid)
    finalize_update(tmp_path, fleet_state='restart_pending')
    monkeypatch.setattr('gateway.status.live_gateway_pid_for_home', lambda home: 1234)
    assert not await runner._send_update_notification()
    assert not wakes(authority, sid)
    runtime_status(tmp_path, monkeypatch)
    # A second launch must not overwrite the unconsumed receipt and exit sentinel.
    spawn = Mock()
    assert not launch_native_update(home=tmp_path, hermes_cmd=['hermes'], spawn=spawn, pending={})['started']
    spawn.assert_not_called()
    assert await runner._send_update_notification()
    rows = wakes(authority, sid)
    assert len(rows) == 1, f'no durable wake; direct sends: {[str(c.args[1]) for c in send.call_args_list]}'
    event = restore_native(rows[0]['payload'], runner)
    assert event.internal and event.reply_expected
    assert event.source.thread_id == '77'
    assert '0.local-proof' in event.text and 'healthy' in event.text and 'aaaaaaaaaaaa' in event.text
    assert not any('Update Complete' in c.args[1] for c in send.call_args_list)
    assert read_pending(tmp_path) is None
    assert not await runner._send_update_notification()
    await async_wait_until(lambda: turn.await_count == 1, timeout=10, what='origin wake turn')


@pytest.mark.asyncio
async def test_failed_update_retries_and_replays_durable_wake(origin, monkeypatch):
    runner, adapter, authority, sid, _, turn, send, tmp_path = origin
    finalize_update(tmp_path, outcome='failed')
    (tmp_path / '.update_process_exit_code').write_text('1')
    runtime_status(tmp_path, monkeypatch, sha='b' * 40, state='degraded')
    runner.adapters.pop(Platform.DISCORD)
    assert not await runner._send_update_notification()
    assert read_pending(tmp_path) and not wakes(authority, sid)
    runner.adapters[Platform.DISCORD] = adapter
    from gateway.update_notifications import save_pending
    record = read_pending(tmp_path)
    assert record is not None
    marker, pending = record
    pending.pop('notice_retry_at', None)
    save_pending(marker, pending)

    with monkeypatch.context() as m:
        # Durable admission commits, but the process never reaches marker cleanup.
        m.setattr(runner, '_clear_update_markers', Mock(return_value=False))
        assert not await runner._send_update_notification()
    rows = wakes(authority, sid)
    assert len(rows) == 1 and read_pending(tmp_path)
    event = restore_native(rows[0]['payload'], runner)
    assert 'failed or could not be verified' in event.text
    assert '0.local-proof' in event.text and 'bbbbbbbbbbbb' in event.text and 'degraded' in event.text
    await async_wait_until(lambda: turn.await_count == 1, timeout=10, what='failed update wake')

    # A fresh runtime snapshot could differ on replay. It must not add a turn.
    runtime_status(tmp_path, monkeypatch, sha='b' * 40)
    assert await runner._send_update_notification()
    assert read_pending(tmp_path) is None
    assert [row['admission_id'] for row in wakes(authority, sid)] == [rows[0]['admission_id']]
    assert turn.await_count == 1
    assert not any('Update Failed' in c.args[1] for c in send.call_args_list)


@pytest.mark.asyncio
async def test_completed_request_does_not_wake_reset_session(origin, monkeypatch):
    runner, _, authority, sid, key, turn, send, tmp_path = origin
    replacement = runner.session_store.reset_session(key)
    assert replacement and replacement.session_id != sid
    finalize_update(tmp_path)
    runtime_status(tmp_path, monkeypatch)
    assert await runner._send_update_notification()
    assert not wakes(authority, sid) and not wakes(authority, replacement.session_id)
    assert turn.await_count == 0 and read_pending(tmp_path) is None
    assert not any('Update Complete' in c.args[1] for c in send.call_args_list)
