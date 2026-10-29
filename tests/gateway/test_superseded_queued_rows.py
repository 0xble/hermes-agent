"""Queued messaging rows never outlive the conversation that /new replaced.

A /new that arrived right after a restart, before startup recovery mounted the route's durable
FIFO, skipped that FIFO's queued rows: the lane moved to a fresh session and the old rows stayed
``queued`` forever. Once superseded, every write (cancel included) was refused as
``session_superseded`` and the ledger refused the delete as ``session_busy``. Real GatewayRunner,
DiscordAdapter and authority over a real SessionDB.
"""
import pytest


def _setup(monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import SessionAuthority
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    return runner, adapter


async def _queue_rows(runner, authority, source, *names):
    from gateway.platforms.event import MessageEvent
    from gateway.session_ingress_context import native_callback
    from hermes_constants import get_hermes_home
    for name in names:
        event = MessageEvent(text=name, source=source, message_id=name)
        with native_callback(runner, event, get_hermes_home()):
            await authority.admit_native(event)
    key = runner.session_store._generate_session_key(source)
    return key, runner.session_store.peek_session_id(key)


def _rows(authority, sid):
    from hermes_state_runtime import list_session_admissions
    return [(r['request_id'], r['status'], r['outcome'])
            for r in list_session_admissions(authority.db, session_id=sid, pending_only=False)]


@pytest.mark.asyncio
async def test_new_before_startup_recovery_cancels_the_routes_queued_rows(monkeypatch):
    from gateway.platforms.event import MessageEvent
    from gateway.session_authority import initialize_session_authority
    from gateway.session_ingress_context import native_callback
    from hermes_constants import get_hermes_home

    runner, adapter = _setup(monkeypatch)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    key, owner = await _queue_rows(runner, authority, source, 'A', 'B')
    assert _rows(authority, owner) == [('A', 'queued', None), ('B', 'queued', None)]

    # A fresh process: the durable rows exist, but startup recovery has not mounted the route yet.
    authority.sessions.pop(owner)
    with native_callback(runner, MessageEvent(text='/new', source=source), get_hermes_home()):
        await runner._handle_reset_command(MessageEvent(text='/new', source=source))  # the confirmed /new

    assert runner.session_store.peek_session_id(key) != owner, '/new moved the lane to a fresh session'
    assert _rows(authority, owner) == [('A', 'terminal', 'cancelled'), ('B', 'terminal', 'cancelled')]


@pytest.mark.asyncio
async def test_a_superseded_sessions_queued_rows_can_be_cancelled_then_deleted(monkeypatch):
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal, SessionRef

    runner, adapter = _setup(monkeypatch)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    key, owner = await _queue_rows(runner, authority, source, 'A', 'B')
    from hermes_state_runtime import list_session_admissions
    queued = [r['admission_id'] for r in list_session_admissions(authority.db, session_id=owner)]
    # The stranded state a pre-fix /new left: the lane moved on, the old rows still queued.
    authority.sessions.pop(owner)
    runner.session_store.reset_session(key)
    assert runner.session_store.peek_session_id(key) != owner

    actor = Principal('messaging-control', authority.profile_id,
                      frozenset({'session:submit', 'session:control'}), 'operator')
    ref = SessionRef(authority.profile_id, owner)
    for admission_id in queued:
        receipt = await authority.cancel_queued(actor, ref, admission_id)
        assert (receipt.status, receipt.outcome) == ('terminal', 'cancelled')
    assert authority.sessions[owner].superseded, 'the old conversation mounted read-only'
    assert _rows(authority, owner) == [('A', 'terminal', 'cancelled'), ('B', 'terminal', 'cancelled')]
    assert authority.db.delete_session(owner, exclude_active_write_guards=True)
