"""Producer prompts traverse the native boundary without granting metadata to human input."""
import asyncio
import json
import logging
from dataclasses import replace

import pytest
import pytest_asyncio

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from gateway.session_envelope import check_native_route, restore_native
from gateway.session_ingress import admit_message
from gateway.session_results import execution_result
from gateway.wake import admit_internal_event
from hermes_constants import get_hermes_home
from hermes_state_runtime import RuntimeStoreError, list_session_admissions
from tests.waiting import async_wait_until


@pytest_asyncio.fixture
async def native_chat(tmp_path, monkeypatch):
    from plugins.platforms.discord.adapter import DiscordAdapter
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False))
    adapter.gateway_runner, runner.adapters = runner, {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='synthetic')
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
        # Admission/dispatch are real. Model inference and the later goal judge are not under test.
        return None

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    monkeypatch.setattr(runner, '_run_post_turn_hooks', post_turn)
    source = adapter.build_source(chat_id='42', user_id='42', chat_type='dm')
    try:
        await adapter.handle_message(MessageEvent(text='human seed', source=source, message_id='seed'))
        await async_wait_until(lambda: len(turns) == 1 and not adapter._background_tasks,
                               timeout=20, what='native human seed')
        yield runner, adapter, authority, source, sent, turns
    finally:
        for task in list(adapter._background_tasks):
            task.cancel()
        await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)
        from gateway.session_runtime_workers import mutation_tasks
        await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task),
                             return_exceptions=True)
        while pending := mutation_tasks(authority):
            await asyncio.gather(*pending, return_exceptions=True)
        authority.db.close()


def rows(authority, sid):
    return list_session_admissions(authority.db, session_id=sid, pending_only=False)


@pytest.mark.asyncio
async def test_lifted_goal_barrier_drives_real_native_admission(native_chat):
    from hermes_cli.goals import GoalManager, load_goal
    runner, adapter, authority, source, sent, turns = native_chat
    key = runner._session_key_for_source(source)
    entry = runner.session_store.lookup_by_session_key(key)
    receipts = get_hermes_home() / 'logs' / 'process-results'
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / 'proc-synthetic.json').write_text(json.dumps({
        'id': 'proc-synthetic', 'exit_code': -15, 'completion_reason': 'killed',
        'termination_source': 'kill_all'}))
    manager = GoalManager(entry.session_id)
    manager.set('Finish the verification')
    manager.wait_on_session('proc-synthetic', reason='waiting for verifier')
    await runner._goal_wakeup_fire_one(entry.session_id)
    await async_wait_until(lambda: not adapter._background_tasks, timeout=20, what='goal admission')
    admitted = rows(authority, entry.session_id)
    assert len(admitted) == 2, (sent, admitted)
    restored = restore_native(admitted[-1]['payload'], runner)
    assert restored.metadata['gateway_session_key'] == key
    assert restored.metadata['goal_continuation_session_id'] == entry.session_id
    assert restored.metadata['goal_continuation'] is True
    assert restored.internal is False  # goal evaluation is not an internal notification turn
    assert restored.reply_expected is False
    assert turns[-1].metadata == restored.metadata
    assert load_goal(entry.session_id).waiting_on_session is None
    await check_native_route(runner, admitted[-1]['payload'], entry.session_id, source, adapter)
    assert not any('refused this message' in text for text in sent)


@pytest.mark.asyncio
async def test_watch_match_drives_real_process_notice_admission(native_chat):
    from tools.process_registry import ProcessRegistry, ProcessSession
    runner, adapter, authority, source, sent, turns = native_chat
    key = runner._session_key_for_source(source)
    entry = runner.session_store.lookup_by_session_key(key)
    registry = ProcessRegistry()
    process = ProcessSession(id='proc-watch', command='verifier', task_id='task',
                             session_key=key, parent_session_id=entry.session_id)
    process.watch_patterns = ['READY']
    registry._check_watch_patterns(process, 'READY\n')
    evt = registry.completion_queue.get_nowait()
    assert await runner._inject_watch_notification('Verifier READY', evt) is True
    await async_wait_until(lambda: len(turns) == 2, timeout=20, what='watch execution')
    admitted = rows(authority, entry.session_id)
    assert len(admitted) == 2
    restored = restore_native(admitted[-1]['payload'], runner)
    assert restored.internal is True
    assert restored.metadata['gateway_session_key'] == key
    assert restored.metadata['gateway_session_id'] == entry.session_id
    assert admitted[-1]['payload']['native_text_v1']['automation']['identity']
    assert not any('refused this message' in text for text in sent)


@pytest.mark.asyncio
async def test_internal_entry_can_use_native_codec_without_losing_metadata(native_chat):
    runner, adapter, authority, source, sent, turns = native_chat
    event = MessageEvent(text='process notice', source=source, internal=True,
                         metadata={'gateway_session_key': runner._session_key_for_source(source),
                                   'notification_origin': 'process_registry_synthetic'})

    # Drive the public producer entry and the real authority, including _validate_native.
    class NativeAdapter:
        gateway_runner = None

        async def handle_message(self, event):
            from gateway.session_ingress_context import native_callback
            with native_callback(runner, event, get_hermes_home()):
                receipt = await authority.admit_native(event)
            event._gateway_accepted = receipt is not None

    await admit_internal_event(NativeAdapter(), event)
    await async_wait_until(lambda: len(turns) == 2, timeout=20, what='native internal execution')
    restored = turns[-1]
    assert restored.internal is True
    assert restored.metadata == event.metadata


@pytest.mark.asyncio
async def test_foreign_user_metadata_still_refuses(native_chat):
    runner, adapter, authority, source, sent, turns = native_chat
    for metadata in ({'foreign': 'value'}, {'gateway_session_key': 'forged'},
                     {'goal_continuation': True}, {'_native_synthetic': True}, ['foreign']):
        with pytest.raises(RuntimeStoreError) as refused:
            await authority.admit_native(MessageEvent(text='human', source=source, metadata=metadata))
        assert (refused.value.reason, refused.value.field) == ('invalid_params', 'metadata')
    assert len(turns) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['internal', 'goal', 'cron', 'process'])
async def test_internal_refusal_has_no_reply_and_one_operator_warning(native_chat, monkeypatch, caplog, shape):
    runner, adapter, authority, source, sent, turns = native_chat
    fields = {'internal': {'internal': True},
              'goal': {'metadata': {'goal_continuation': True}},
              'cron': {'metadata': {'cron_delivery': True}},
              'process': {'metadata': {'notification_origin': 'process_registry_synthetic'}}}[shape]
    clock = [1000.0]
    monkeypatch.setattr('gateway.session_refusals.monotonic', lambda: clock[0])
    event = MessageEvent(text='bad synthetic', source=source, media_urls='not-a-list', **fields)
    with caplog.at_level(logging.DEBUG):
        replies = [await admit_message(authority, event) for _ in range(5)]
    assert replies == [None] * 5
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert 'invalid_params' in warnings[0].getMessage()
    assert 'counts=' in warnings[0].getMessage()
    clock[0] += 600
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert await admit_message(authority, event) is None
    assert len(caplog.records) == 1
    assert 'previous_window=' in caplog.records[0].getMessage()
    assert len(turns) == 1


@pytest.mark.asyncio
async def test_user_refusal_notice_is_per_chat_reason_and_ten_minutes(native_chat, monkeypatch, caplog):
    runner, adapter, authority, source, sent, turns = native_chat
    clock = [1000.0]
    monkeypatch.setattr('gateway.session_refusals.monotonic', lambda: clock[0])
    event = MessageEvent(text='bad user input', source=source, media_urls='not-a-list')
    assert 'refused this message' in await admit_message(authority, event)
    with caplog.at_level(logging.INFO):
        for _ in range(4):
            assert await admit_message(authority, event) is None
    assert any('suppressed=4' in record.getMessage() for record in caplog.records)
    # Threads share their parent chat's budget. Another chat does not.
    assert await admit_message(authority, replace(event, source=replace(source, thread_id='other'))) is None
    other = replace(event, source=replace(source, chat_id='other'))
    assert 'refused this message' in await admit_message(authority, other)
    clock[0] += 599
    assert await admit_message(authority, event) is None
    clock[0] += 1
    assert 'refused this message' in await admit_message(authority, event)


@pytest.mark.asyncio
async def test_refused_gateway_prompt_never_sends_a_transport_reply(native_chat):
    runner, adapter, authority, source, sent, turns = native_chat
    before = list(sent)
    event = runner._synthetic_prompt_event(source, 'gateway wake', goal_continuation=True)
    event.media_urls = 'invalid'
    await admit_internal_event(adapter, event)
    await async_wait_until(lambda: not adapter._background_tasks, timeout=20, what='refused synthetic dispatch')
    assert sent == before
    assert len(turns) == 1
    entry = runner.session_store.lookup_by_session_key(runner._session_key_for_source(source))
    assert len(rows(authority, entry.session_id)) == 1


@pytest.mark.asyncio
async def test_rewritten_gateway_prompt_keeps_private_producer_provenance(native_chat):
    from gateway.session_ingress_context import carry_callback, native_callback
    runner, adapter, authority, source, sent, turns = native_chat
    original = runner._synthetic_prompt_event(source, 'original', goal_continuation=True)
    rewritten = replace(original, text='rewritten')
    carry_callback(original, rewritten)
    with native_callback(runner, rewritten, get_hermes_home()):
        await authority.admit_native(rewritten)
    await async_wait_until(lambda: len(turns) == 2, timeout=20, what='rewritten producer admission')
    assert turns[-1].text == 'rewritten'
    assert turns[-1].metadata == original.metadata
    # Changing sources cannot transfer private provenance to an unrelated event.
    foreign = replace(original, source=replace(source, chat_id='other'))
    carry_callback(original, foreign)
    with pytest.raises(RuntimeStoreError) as refused:
        await authority.admit_native(foreign)
    assert refused.value.field == 'metadata'
