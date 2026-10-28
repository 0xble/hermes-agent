"""First authority boot must accept existing direct-chat process work without a new human turn."""
import asyncio
from collections import deque

import pytest
import pytest_asyncio

from gateway import admission_canary
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from gateway.session_results import execution_result
from hermes_state_runtime import list_session_admissions
from tests.waiting import async_wait_until


@pytest_asyncio.fixture(params=['discord', 'telegram'])
async def upgraded_chat(tmp_path, monkeypatch, request):
    from plugins.platforms.discord.adapter import DiscordAdapter
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from tools.process_registry import ProcessRegistry, ProcessSession

    platform = Platform(request.param)
    monkeypatch.setenv(f'{platform.value.upper()}_ALLOWED_USERS', '42')
    monkeypatch.setattr(admission_canary, '_events', deque(maxlen=4096))
    config = GatewayConfig(sessions_dir=tmp_path / 'sessions')
    previous = GatewayRunner(config)
    adapter_type = DiscordAdapter if platform == Platform.DISCORD else TelegramAdapter
    adapter = adapter_type(PlatformConfig(enabled=True, token='123:fixture', typing_indicator=False))
    source = adapter.build_source(chat_id='42', user_id='42', chat_type='dm', thread_id='7')
    entry = previous.session_store.get_or_create_session(source)
    registry = ProcessRegistry()
    process = ProcessSession(id='upgrade-watch', command='verifier', task_id='task',
                             session_key=entry.session_key, parent_session_id=entry.session_id)
    process.watch_patterns = ['READY']
    registry._check_watch_patterns(process, 'READY\n')
    queued = registry.completion_queue.get_nowait()
    previous.close_all_session_db_handles()

    # Rebuild both the SessionStore and authority from the persisted pre-upgrade route.
    runner = GatewayRunner(config)
    runner.adapters = {platform: adapter}
    adapter.gateway_runner = runner
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='upgrade')
    runner._wire_adapter_handlers(adapter)
    sent, turns = [], []

    async def send(chat_id, content, **kwargs):
        sent.append(content)
        return SendResult(success=True, message_id='sent')

    async def turn(event, *args, **kwargs):
        turns.append(event)
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'

    async def post_turn(**kwargs):
        return None

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    monkeypatch.setattr(runner, '_run_post_turn_hooks', post_turn)
    try:
        assert list_session_admissions(authority.db, session_id=entry.session_id, pending_only=False) == []
        yield runner, adapter, authority, entry, queued, turns, sent
    finally:
        from gateway.session_runtime_workers import mutation_tasks
        await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task),
                             return_exceptions=True)
        while pending := mutation_tasks(authority):
            await asyncio.gather(*pending, return_exceptions=True)
        authority.db.close()


@pytest.mark.asyncio
async def test_queued_watch_after_upgrade_needs_no_fresh_human_envelope(upgraded_chat):
    runner, adapter, authority, entry, queued, turns, sent = upgraded_chat
    assert await runner._inject_watch_notification('Verifier READY', queued) is True
    await async_wait_until(lambda: len(turns) == 1, timeout=20, what='upgraded process watch')
    rows = list_session_admissions(authority.db, session_id=entry.session_id, pending_only=False)
    assert len(rows) == 1
    assert rows[0]['principal_id'] == 'automation:' + entry.session_key
    assert rows[0]['payload']['native_text_v1']['automation']
    assert turns[0].internal is True
    assert admission_canary.snapshot()['refusals'] == []
    assert not any('refused' in message for message in sent)


@pytest.mark.asyncio
async def test_upgrade_revalidates_current_direct_chat_authorization(upgraded_chat, monkeypatch):
    runner, _, _, entry, queued, turns, _ = upgraded_chat
    monkeypatch.setenv(f'{entry.origin.platform.value.upper()}_ALLOWED_USERS', 'someone-else')
    assert await runner._inject_watch_notification('Verifier READY', queued) is False
    assert turns == []
    assert admission_canary.snapshot()['refusals'][-1][1] == 'automation: permission_denied (user_not_authorized)'


@pytest.mark.asyncio
async def test_suspended_route_is_refused_without_counting_live_work_outage(upgraded_chat):
    runner, _, _, entry, queued, turns, _ = upgraded_chat
    runner.session_store.lookup_by_session_key(entry.session_key).suspended = True
    assert await runner._inject_watch_notification('Verifier READY', queued) is False
    assert turns == []
    assert admission_canary.snapshot()['refusals'] == []


@pytest.mark.asyncio
async def test_missing_legacy_envelope_does_not_hide_invalid_live_automation(upgraded_chat):
    from gateway.platforms.event import MessageEvent
    from hermes_state_runtime import RuntimeStoreError
    runner, adapter, authority, entry, _, turns, _ = upgraded_chat
    event = MessageEvent(text='notice', source=entry.origin, internal=True,
                         metadata={'gateway_session_key': entry.session_key,
                                   'gateway_session_id': entry.session_id, 'foreign': True})
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        await authority.admit_automation(adapter, event, 'bad-notice')
    assert admission_canary.snapshot()['refusals'][-1][1] == 'automation: invalid_params (automation_metadata)'
    assert turns == []


@pytest.mark.asyncio
@pytest.mark.parametrize('gone', [False, True])
@pytest.mark.parametrize('proof', ['same', 'foreign', 'absent'])
async def test_missing_route_only_excludes_a_proven_gone_target(upgraded_chat, monkeypatch, gone, proof):
    from gateway.platforms.event import MessageEvent
    from hermes_state_runtime import RuntimeStoreError
    runner, adapter, authority, entry, _, turns, _ = upgraded_chat
    source = runner._restored_source(runner.session_store.lookup_by_session_key(entry.session_key))
    if proof == 'foreign':
        from dataclasses import replace
        from gateway.session_identity import identity_of, proven_runtime_home
        home = proven_runtime_home(source)
        assert home is not None
        foreign = home / 'foreign'
        # Standalone restore does not pin a RoutingIdentity. Move whichever
        # proof exists so a foreign home cannot borrow this runtime's exclusion.
        source._restored_runtime_home = foreign
        identity = identity_of(source)
        if identity is not None:
            source._identity = replace(identity, runtime_home=foreign)
    elif proof == 'absent':
        from gateway.session import SessionSource
        source = SessionSource.from_dict(source.to_dict())
    event = MessageEvent(text='notice', source=source, internal=True,
                         metadata={'gateway_session_key': entry.session_key,
                                   'gateway_session_id': 'deleted' if gone else entry.session_id})
    monkeypatch.setattr(runner.session_store, 'lookup_by_session_key', lambda key: None)
    with pytest.raises(RuntimeStoreError) as refused:
        await authority.admit_automation(adapter, event, 'pending-route')
    assert refused.value.reason == 'not_found'
    if gone and proof == 'same':
        assert refused.value.field == 'automation_target_gone'
        assert admission_canary.snapshot()['refusals'] == []
    else:
        assert refused.value.field == 'automation_route_unavailable'
        assert admission_canary.snapshot()['refusals'][-1][1] == 'automation: not_found (automation_route_unavailable)'
    assert turns == []


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['transport', 'runtime', 'roles', 'relay', 'upstream'])
async def test_legacy_origin_cannot_borrow_unproven_connector_or_delegated_grants(upgraded_chat, monkeypatch, changed):
    runner, adapter, authority, entry, queued, turns, _ = upgraded_chat
    restored = runner.session_store.lookup_by_session_key(entry.session_key)
    if changed == 'transport':
        runner.adapters = {}
        # Drive the authority even when producer preflight sees no adapter.
        from gateway.platforms.event import MessageEvent
        from hermes_state_runtime import RuntimeStoreError
        event = MessageEvent(text='notice', source=entry.origin, internal=True,
                             metadata={'gateway_session_key': entry.session_key,
                                       'gateway_session_id': entry.session_id})
        with pytest.raises(RuntimeStoreError):
            await authority.admit_automation(adapter, event, 'lost-transport')
    else:
        if changed == 'runtime':
            from gateway import session_ingress_context
            original = session_ingress_context._binding
            def foreign_home(*args):
                bound, home, runtime, provenance = original(*args)
                return bound, home, runtime / 'foreign', provenance
            monkeypatch.setattr(session_ingress_context, '_binding', foreign_home)
        elif changed == 'roles':
            restored.origin.role_authorized = True
        elif changed == 'relay':
            restored.origin.delivered_via_upstream_relay = True
        else:
            original_flag = runner._adapter_flag
            monkeypatch.setattr(runner, '_adapter_flag', lambda platform, name, profile=None:
                                name == 'authorization_is_upstream' or original_flag(platform, name, profile))
        if changed in {'roles', 'relay', 'upstream'}:
            from gateway import session_ingress_context
            monkeypatch.setattr(session_ingress_context, '_binding',
                                lambda *args: pytest.fail('legacy route minted delegated provenance'))
        assert await runner._inject_watch_notification('Verifier READY', queued) is False
    assert turns == []
    assert len(admission_canary.snapshot()['refusals']) == (
        0 if changed in {'roles', 'relay', 'upstream'} else 1)
