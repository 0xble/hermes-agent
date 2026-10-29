"""A chat's FIFO never waits on a settled media turn's housekeeping.

Settling a photo or voice turn released its captured bytes inline, and that release reads every
transcript row to find a history owner: minutes on a large store, during which the drain neither
sent the settled reply nor claimed the next queued input. Real GatewayRunner, DiscordAdapter,
SessionAuthority and drain over SQLite; the turn itself is a stand-in, the slow scan is held open.
"""
import asyncio
import threading

import pytest

from tests.waiting import async_wait_until


def _harness(monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    sent = []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SendResult(success=True, message_id='sent')
    monkeypatch.setattr(adapter, 'send', send)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    return runner, adapter, sent


def _media_event(source, kind, message_id):
    from gateway.platforms.event import MessageEvent, MessageType
    from hermes_constants import get_hermes_home
    cache = get_hermes_home() / 'cache'
    cache.mkdir(parents=True, exist_ok=True)
    if kind == 'photo':
        path = cache / f'img_{message_id}.png'
        path.write_bytes(b'\x89PNG\r\n\x1a\n' + message_id.encode() * 64)
        return MessageEvent(text='', source=source, message_id=message_id, message_type=MessageType.PHOTO,
                            media_urls=[str(path)], media_types=['image/png'])
    path = cache / f'audio_{message_id}.ogg'
    path.write_bytes(b'OggS' + message_id.encode() * 64)
    return MessageEvent(text='', source=source, message_id=message_id, message_type=MessageType.VOICE,
                        media_urls=[str(path)], media_types=['audio/ogg'])


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['photo', 'voice'])
async def test_text_queued_behind_a_media_turn_runs_while_its_media_release_scans(monkeypatch, kind):
    """Both rows are committed before the drain starts (a restart's recovered FIFO): the text row
    runs, and the media turn's reply is delivered, while the media release is still scanning."""
    from gateway import session_ingress_media
    from gateway.platforms.event import MessageEvent
    from gateway.session_authority import SessionAuthority, initialize_session_authority
    from gateway.session_ingress_context import native_callback
    from gateway.session_results import execution_result
    from hermes_constants import get_hermes_home
    from hermes_state_runtime import list_session_admissions

    runner, adapter, sent = _harness(monkeypatch)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')

    ran = []

    async def turn(event, source, quick_key, run_generation):
        ran.append(event.message_id)
        captured = execution_result.get()
        if captured is not None:
            captured['result'] = {'final_response': f'answer {event.message_id}', 'messages': []}
        return f'answer {event.message_id}'
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)

    # The history-owner scan over every transcript row: held open, as on a large store.
    scanning, finish_scan = threading.Event(), threading.Event()
    real_mentions = session_ingress_media._history_mentions

    def slow_mentions(*args, **kwargs):
        scanning.set()
        assert finish_scan.wait(30), 'the scan was never released'
        return real_mentions(*args, **kwargs)
    monkeypatch.setattr(session_ingress_media, '_history_mentions', slow_mentions)

    schedule = SessionAuthority._schedule
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    receipts = []
    for event in (_media_event(source, kind, 'M'), MessageEvent(text='after the media', source=source,
                                                                message_id='T')):
        with native_callback(runner, event, get_hermes_home()):
            receipts.append(await authority.admit_native(event))
    ref = receipts[0].ref

    def rows():
        return [(r['request_id'], r['status'], r['outcome'])
                for r in list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)]
    media_row, = [r for r in list_session_admissions(authority.db, session_id=ref.session_id)
                  if r['request_id'] == 'M']
    assert media_row['payload']['native_text_v1']['media'], 'the media turn must own captured bytes'
    monkeypatch.setattr(SessionAuthority, '_schedule', schedule)
    try:
        authority._schedule(ref)
        await async_wait_until(lambda: rows() == [('M', 'terminal', 'completed'), ('T', 'terminal', 'completed')],
                               timeout=10, what='the text row to run while the media release scans')
        assert ran == ['M', 'T']
        assert scanning.is_set(), 'the media release ran, off the FIFO path'
        await async_wait_until(lambda: {'answer M', 'answer T'} <= set(sent), timeout=5,
                               what='both settled replies to be delivered')
        assert runner._peek_session_state(authority.sessions[ref.session_id].route).turn.agent is None
    finally:
        finish_scan.set()
    from gateway.session_ingress_media import media_releases_idle
    await asyncio.wait_for(media_releases_idle(authority), 10)
