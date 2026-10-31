"""A claimed FIFO turn the outbox discards as a redelivery never ran, so its admission must fail."""
import logging

import pytest


@pytest.mark.asyncio
async def test_claimed_turn_dropped_as_outbox_duplicate_settles_failed(monkeypatch, caplog):
    from gateway import run_inbound
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from tests.fakes.runtime_owner import initialize_session_authority
    from gateway.session_ingress import executing_admission
    from hermes_state_runtime import list_session_admissions
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '42')
    config = GatewayConfig()
    config.durable_outbox_enabled = True
    runner = GatewayRunner(config)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    adapter.gateway_runner = runner
    runner.adapters = {Platform.TELEGRAM: adapter}
    monkeypatch.setattr(runner, '_read_user_config', dict)
    monkeypatch.setattr(adapter, '_start_typing_refresh', lambda *a, **kw: None)
    sent, turns = [], []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SendResult(success=True, message_id=f's{len(sent)}')

    async def turn(event, source, quick_key, run_generation):
        turns.append(event.message_id)
        return 'ran'

    # Reproduce the 2026-10-29 failure mode regardless of how execution identity is derived:
    # the claimed execution's outbox admission comes back as an already-admitted redelivery.
    original_admit = run_inbound._admit_outbox_event

    async def admit(runner_, event, source):
        await original_admit(runner_, event, source)
        if executing_admission.get():
            event._outbox_duplicate = True

    monkeypatch.setattr(run_inbound, '_admit_outbox_event', admit)
    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42', thread_id='336411')
    key = runner.session_store._generate_session_key(source)

    event = MessageEvent(text='Walkthrough this with me', source=source, message_id='m1', platform_update_id=950)
    with caplog.at_level(logging.ERROR, logger='gateway.session_ingress'):
        await adapter.handle_message(event)
        await adapter._session_tasks[adapter._event_session_key(event)]

    sid = runner.session_store.peek_session_id(key)
    rows = list_session_admissions(authority.db, session_id=sid, pending_only=False)
    assert turns == []
    assert sent == []
    assert [(row['status'], row['outcome']) for row in rows] == [('terminal', 'failed')]
    assert any('produced no turn' in record.getMessage() for record in caplog.records)
