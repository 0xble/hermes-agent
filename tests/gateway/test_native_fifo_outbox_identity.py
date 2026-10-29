"""Durable Telegram ingress and its claimed FIFO execution are different admissions."""
import asyncio

import pytest

from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_telegram_fifo_runs_task_wakes_once_without_ingress_redelivery(monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.outbox import Outbox
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_ingress import executing_admission
    from gateway.session_results import execution_result
    from hermes_constants import get_hermes_home
    from hermes_state_runtime import list_session_admissions
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '42')
    config = GatewayConfig()
    config.durable_outbox_enabled = True
    runner = GatewayRunner(config)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    adapter.gateway_runner = runner
    runner.adapters = {Platform.TELEGRAM: adapter}
    monkeypatch.setattr(runner, '_read_user_config', lambda: {})
    monkeypatch.setattr(adapter, '_start_typing_refresh', lambda *a, **kw: None)
    sent, turns = [], []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SendResult(success=True, message_id=f's{len(sent)}')

    async def turn(event, source, quick_key, run_generation):
        turns.append((event.message_id, executing_admission.get(), event._outbox_turn_id))
        result = {'final_response': 'audited ' + event.message_id, 'messages': []}
        execution_result.get()['result'] = result
        return result['final_response']

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42', thread_id='311115')
    key = runner.session_store._generate_session_key(source)

    def wake(message_id, update_id):
        return MessageEvent(
            text=('Audit maintenance\n[relay from=hermes:default/cron_audit '
                  f'receipt={message_id} task=shared-maintenance-task]'),
            source=source, message_id=message_id, platform_update_id=update_id)

    async def rows():
        sid = runner.session_store.peek_session_id(key)
        return [] if sid is None else list_session_admissions(authority.db, session_id=sid, pending_only=False)

    # The task is intentionally shared, but provider occurrences are distinct.
    for message_id, update_id in [('audit', 933), ('window', 934)]:
        await adapter.handle_message(wake(message_id, update_id))
        await async_wait_until(
            lambda: len(sent) >= (1 if message_id == 'audit' else 2) or
                    bool(authority.sessions and all(not live.task or live.task.done()
                                                    for live in authority.sessions.values())),
            timeout=10, what='native FIFO wake to settle')
        # Admission alone is not success: the actual turn body must have run.
        assert any(item[0] == message_id for item in turns), await rows()

    assert [(message_id, claimed) for message_id, claimed, _ in turns] == [('audit', True), ('window', True)]
    assert len({turn_id for _, _, turn_id in turns}) == 2
    assert sent == ['audited audit', 'audited window']
    # A genuine duplicate remains suppressed, rather than bypassing outbox dedup globally.
    await adapter.handle_message(wake('audit', 933))
    await asyncio.sleep(0)
    assert len(turns) == 2
    assert len(await rows()) == 2
    store = Outbox(get_hermes_home())
    for row, (_, _, turn_id) in zip(await rows(), turns):
        assert store.lookup('default', 'telegram', 'admission:' + row['admission_id'], 'text') == (turn_id, 'completed')
