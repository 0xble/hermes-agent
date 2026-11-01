"""Every turn's temporary progress bubbles are deleted when that turn finishes, whatever path ran it.

A turn registers its bubble cleanup as a post-delivery callback keyed by its run generation
(``_run_agent_schedule_bubble_cleanup``); whoever delivers the turn's reply must fire it. That
owner differs by path: the first message's adapter lifecycle, the observer of a follow-up sent
while the session is busy (``dispatch_shared_busy``), or the FIFO drain itself for a turn with no
live waiter (producer wakes, recovered turns). A path that never fires it leaves its tool cards in
the chat for good (2026-10-31: the follow-up path did, topic 410205). Real GatewayRunner, adapter,
SessionAuthority, drain and ``execute_admission`` over SQLite; only the agent turn is a stand-in
that registers its cleanup the way the real turn does.
"""
import asyncio

import pytest

from tests.gateway.test_shared_busy_receive_loop import Adapter, _statuses, _wait_for


async def _owner(monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session_results import execution_result
    from tests.fakes.runtime_owner import initialize_session_authority

    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'fixture-user')
    runner = GatewayRunner(GatewayConfig())
    adapter = Adapter()
    adapter.gateway_runner = runner
    runner.adapters = {Platform.TELEGRAM: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='busy-chat', chat_type='dm', user_id='fixture-user')
    gates, cleaned = {}, []

    async def turn(event, source, quick_key, run_generation):
        text = str(event.text)
        gates.setdefault('_entered', set()).add(text)
        if text in gates:
            await gates[text].wait()
        if text == 'released early':
            # Its waiter is released before the turn ends, as a pause releases a session's waiters.
            from hermes_state_runtime import RuntimeStoreError
            authority.waiters.pop(event._outbox_admission_id.split(':', 1)[1]).set_exception(
                RuntimeStoreError('unknown_execution'))
            await asyncio.sleep(0)

        def cleanup():
            cleaned.append((text, any(sent == 'reply to ' + text for sent in adapter.sent)))
        adapter.register_post_delivery_callback(quick_key, cleanup, generation=run_generation)
        captured = execution_result.get()
        if captured is not None:  # a FIFO-run turn; an in-process turn has no drain to tell
            captured['post_delivery'] = (adapter, quick_key, run_generation)
            captured['result'] = {'final_response': 'reply to ' + text, 'messages': []}
        return 'reply to ' + text
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    return runner, authority, adapter, source, gates, cleaned


def _held(adapter):
    return dict(adapter._post_delivery_callbacks_by_generation), dict(adapter._post_delivery_callbacks)


async def _settled(authority, adapter, cleaned):
    """Nothing left to fire, and nothing fired twice (a double fire would delete twice)."""
    await _wait_for(lambda: not adapter._background_tasks, 15)
    await asyncio.sleep(0.2)  # room for a late duplicate fire
    texts = [text for text, _ in cleaned]
    assert len(texts) == len(set(texts)), f'a cleanup fired twice: {cleaned}'
    assert _held(adapter) == ({}, {})
    assert not getattr(authority, 'waiter_post_deliveries', {})
    assert not getattr(authority, 'post_deliveries', {})
    assert not getattr(adapter, '_post_delivery_claims', set())


@pytest.mark.asyncio
async def test_every_turn_path_deletes_its_own_bubbles(monkeypatch):
    from gateway.platforms.event import MessageEvent
    from gateway.session_automation import admit_route_wake

    runner, authority, adapter, source, gates, cleaned = await _owner(monkeypatch)
    gates['first'] = asyncio.Event()
    # Adapter lifecycle: the first message, while the session is idle.
    await adapter.handle_message(MessageEvent(text='first', source=source, message_id='m1'))
    await _wait_for(lambda: _statuses(authority).get('first') == 'started')
    # Busy follow-up: a user message sent while 'first' runs.
    await asyncio.wait_for(adapter.handle_message(MessageEvent(text='follow-up', source=source, message_id='m2')), 5)
    gates['first'].set()
    await _wait_for(lambda: {'first', 'follow-up'} <= {text for text, _ in cleaned}, 15)
    # FIFO drain with no live waiter: a producer wake (/loop tick, watch notification), delivered
    # by the drain like a recovered turn.
    assert await admit_route_wake(runner, adapter, source, 'loop tick', 'loop')
    await _wait_for(lambda: 'loop tick' in {text for text, _ in cleaned}, 15)
    await _wait_for(lambda: not adapter._background_tasks, 15)
    assert sorted(cleaned) == sorted([('first', True), ('follow-up', True), ('loop tick', True)])
    assert _held(adapter) == ({}, {})
    assert not getattr(authority, 'waiter_post_deliveries', {})


@pytest.mark.asyncio
async def test_a_waiter_that_stops_waiting_still_deletes_its_turns_bubbles(monkeypatch):
    """A busy follow-up whose observer is cancelled sends no reply, so the settling drain fires the
    turn's cleanup instead of leaving it held."""
    from gateway.platforms.event import MessageEvent

    _, authority, adapter, source, gates, cleaned = await _owner(monkeypatch)
    gates['first'], gates['follow-up'] = asyncio.Event(), asyncio.Event()
    await adapter.handle_message(MessageEvent(text='first', source=source, message_id='m1'))
    await _wait_for(lambda: _statuses(authority).get('first') == 'started')
    observers = set(adapter._background_tasks)
    await asyncio.wait_for(adapter.handle_message(MessageEvent(text='follow-up', source=source, message_id='m2')), 5)
    (observer,) = {task for task in set(adapter._background_tasks) - observers
                   if task.get_coro().__qualname__.endswith('observe')}
    gates['first'].set()
    await _wait_for(lambda: _statuses(authority).get('follow-up') == 'started', 15)
    observer.cancel()
    gates['follow-up'].set()
    await _wait_for(lambda: 'follow-up' in {text for text, _ in cleaned}, 15)
    assert ('follow-up', False) in cleaned
    assert _held(adapter) == ({}, {})
    assert not getattr(authority, 'waiter_post_deliveries', {})


@pytest.mark.asyncio
async def test_a_waiter_released_before_its_turn_ends_still_deletes_its_bubbles(monkeypatch):
    """A busy follow-up whose waiter was released while the turn still ran (a pause) claims nothing
    afterwards, so the drain fires the turn's cleanup itself."""
    from gateway.platforms.event import MessageEvent

    _, authority, adapter, source, gates, cleaned = await _owner(monkeypatch)
    gates['first'] = asyncio.Event()
    await adapter.handle_message(MessageEvent(text='first', source=source, message_id='m1'))
    await _wait_for(lambda: _statuses(authority).get('first') == 'started')
    await asyncio.wait_for(adapter.handle_message(MessageEvent(text='released early', source=source, message_id='m2')), 5)
    gates['first'].set()
    await _wait_for(lambda: 'released early' in {text for text, _ in cleaned}, 15)
    await _wait_for(lambda: not adapter._background_tasks, 15)
    assert _held(adapter) == ({}, {})
    assert not getattr(authority, 'waiter_post_deliveries', {})


@pytest.mark.asyncio
async def test_a_superseded_turn_deletes_its_cards_but_drops_its_notices():
    """A newer run generation superseded the turn, so its result and deferred notices are dropped,
    but the temporary cards it already sent are still deleted."""
    from types import SimpleNamespace
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.run_turn import GatewayTurnMixin
    from plugins.platforms.telegram.chat_budget import keep_trigger
    from tests.gateway.test_post_delivery_callback_chaining import _MinAdapter

    class Telegramish(_MinAdapter):
        """Wraps registrations like TelegramAdapter, so the cleanup sits inside a chain of wrappers."""
        deleted: list

        def register_post_delivery_callback(self, session_key, callback, *, generation=None):
            super().register_post_delivery_callback(session_key, keep_trigger(callback), generation=generation)

        async def delete_message(self, chat_id, message_id):
            self.deleted.append(message_id)
            return True

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True, message_id='1')

    adapter = Telegramish(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.deleted = []
    notices = []
    adapter.register_post_delivery_callback('k', lambda: notices.append('review notice'), generation=7)
    runner = SimpleNamespace(_delivery_adapter_for=lambda source: adapter,
                             _pop_post_delivery_callback=GatewayTurnMixin._pop_post_delivery_callback)
    source = SimpleNamespace(chat_id='c')
    turn_ctx = SimpleNamespace(
        _cleanup_msg_ids=['card-1'], session_key='k', _status_delivery=None, _bubble_cleanup_registered=False,
        _cleanup_progress=True, _post_delivery_adapter=None, source=source, run_generation=7)
    GatewayTurnMixin._run_agent_schedule_bubble_cleanup(runner, {'final_response': 'ok'}, adapter, turn_ctx)
    turn_ctx._cleanup_msg_ids.append('card-2')  # a card whose receipt landed after registration
    GatewayTurnMixin._hmwa_discard_stale_result(runner, source, 'k', 7)
    await _wait_for(lambda: len(adapter.deleted) == 2, 5)
    assert sorted(adapter.deleted) == ['card-1', 'card-2']
    assert notices == []
    assert _held(adapter) == ({}, {})


@pytest.mark.asyncio
async def test_a_queued_followups_busy_ack_is_deleted_once_its_reply_is_delivered(monkeypatch):
    """The ``⏳ Queued for the next turn`` ack is status about the follow-up: it goes once the
    follow-up's own reply is in the chat. Replies themselves are never deleted."""
    from gateway.platforms.event import MessageEvent
    from gateway.run_turn_lease_wait import lease_wait_notice

    runner, authority, adapter, source, gates, cleaned = await _owner(monkeypatch)
    deleted = []

    async def delete_message(chat_id, message_id):
        deleted.append(adapter.sent[int(message_id.split('-')[1]) - 1])
        return True
    adapter.delete_message = delete_message

    async def acknowledge(event, source, quick_key):
        # The busy resolution acknowledges a queued follow-up with the shared queued notice.
        await lease_wait_notice(runner, quick_key, event)
        return False
    gates['first'] = asyncio.Event()
    await adapter.handle_message(MessageEvent(text='first', source=source, message_id='m1'))
    await _wait_for(lambda: 'first' in gates.get('_entered', ()))
    # 'first' is running now: the stand-in registers no agent, so say so for this busy admission.
    runner._is_session_running = lambda key: True
    runner._route_plaintext_approval_while_busy = lambda event, key: _false()
    runner._hm_busy_preempt = acknowledge
    try:
        await asyncio.wait_for(adapter.handle_message(MessageEvent(text='follow-up', source=source, message_id='m2')), 5)
        await _wait_for(lambda: _statuses(authority).get('follow-up') == 'queued', 5)
    finally:
        for name in ('_is_session_running', '_route_plaintext_approval_while_busy', '_hm_busy_preempt'):
            del runner.__dict__[name]
    await _wait_for(lambda: any(sent.startswith('⏳ Queued') for sent in adapter.sent), 5)
    ack = next(sent for sent in adapter.sent if sent.startswith('⏳ Queued'))
    assert not deleted, 'the ack stays while its turn waits'
    gates['first'].set()
    await _wait_for(lambda: deleted == [ack], 15)
    await _settled(authority, adapter, cleaned)
    assert deleted == [ack]
    assert {'reply to first', 'reply to follow-up'} <= set(adapter.sent)


async def _false():
    return False


@pytest.mark.asyncio
async def test_stop_cancelling_the_adapter_task_still_deletes_the_turns_bubbles(monkeypatch):
    """/stop cancels the first message's adapter task while its turn runs: no reply comes, but the
    turn's cards still go once it ends."""
    from gateway.platforms.event import MessageEvent

    runner, authority, adapter, source, gates, cleaned = await _owner(monkeypatch)
    gates['first'] = asyncio.Event()
    await adapter.handle_message(MessageEvent(text='first', source=source, message_id='m1'))
    await _wait_for(lambda: _statuses(authority).get('first') == 'started')
    (task,) = [task for task in adapter._session_tasks.values() if not task.done()]
    task.cancel()
    gates['first'].set()
    await _wait_for(lambda: 'first' in {text for text, _ in cleaned}, 15)
    await _settled(authority, adapter, cleaned)


@pytest.mark.asyncio
async def test_a_shutdown_release_of_the_waiters_still_deletes_the_turns_bubbles(monkeypatch):
    """Draining for shutdown releases the session's waiters while a follow-up's turn runs."""
    from gateway.platforms.event import MessageEvent
    from gateway.session_contract import SessionRef

    runner, authority, adapter, source, gates, cleaned = await _owner(monkeypatch)
    gates['first'], gates['follow-up'] = asyncio.Event(), asyncio.Event()
    await adapter.handle_message(MessageEvent(text='first', source=source, message_id='m1'))
    await _wait_for(lambda: _statuses(authority).get('first') == 'started')
    await asyncio.wait_for(adapter.handle_message(MessageEvent(text='follow-up', source=source, message_id='m2')), 5)
    gates['first'].set()
    await _wait_for(lambda: _statuses(authority).get('follow-up') == 'started', 15)
    (session_id,) = authority.sessions
    authority._pause(SessionRef(authority.profile_id, session_id), 'runtime_draining')
    gates['follow-up'].set()
    await _wait_for(lambda: {'first', 'follow-up'} <= {text for text, _ in cleaned}, 15)
    await _settled(authority, adapter, cleaned)


@pytest.mark.asyncio
async def test_an_unknown_settlement_still_deletes_the_turns_bubbles(monkeypatch):
    """Settlement fails and the turn is fenced unknown: its reply is held for a Discard decision and
    its waiter gets a pause notice, but its cards go."""
    import gateway.session_settlement_recovery as recovery
    from gateway.platforms.event import MessageEvent

    runner, authority, adapter, source, gates, cleaned = await _owner(monkeypatch)
    settle = recovery.settle_with_retry

    async def failing(authority_, live, ref, row, *args, **kwargs):
        if row['payload'].get('text') == 'unsettled' or 'unsettled' in str(row['payload']):
            raise RuntimeError('settlement write failed')
        return await settle(authority_, live, ref, row, *args, **kwargs)
    monkeypatch.setattr(recovery, 'settle_with_retry', failing)
    await adapter.handle_message(MessageEvent(text='unsettled', source=source, message_id='m1'))
    await _wait_for(lambda: 'unsettled' in {text for text, _ in cleaned}, 15)
    assert 'reply to unsettled' not in adapter.sent
    await _settled(authority, adapter, cleaned)


@pytest.mark.asyncio
async def test_a_waiter_registered_after_its_turn_started_deletes_the_bubbles_after_the_reply(monkeypatch):
    """The drain claimed the row before ``admit_message`` registered its waiter, so the drain hands
    the reply over; the cleanup follows that reply instead of racing it."""
    from gateway.platforms.event import MessageEvent

    runner, authority, adapter, source, gates, cleaned = await _owner(monkeypatch)
    gates['late'] = asyncio.Event()
    admit = authority.admit_native

    async def slow_admit(event):
        receipt = await admit(event)
        await _wait_for(lambda: _statuses(authority).get('late') == 'started', 5)
        return receipt
    monkeypatch.setattr(authority, 'admit_native', slow_admit)
    await adapter.handle_message(MessageEvent(text='late', source=source, message_id='m1'))
    await _wait_for(lambda: authority.native_waiters, 5)
    gates['late'].set()
    await _wait_for(lambda: 'late' in {text for text, _ in cleaned}, 15)
    assert cleaned == [('late', True)]
    await _settled(authority, adapter, cleaned)


@pytest.mark.asyncio
async def test_a_newer_turn_never_deletes_an_older_turns_cards_before_its_reply(monkeypatch):
    """Turn A's reply send outlasts the drain's bounded marker wait, so follow-up B runs and is
    answered first. B's fire must not sweep A's cleanup ahead of A's reply."""
    import gateway.session_authority as session_authority
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent

    runner, authority, adapter, source, gates, cleaned = await _owner(monkeypatch)
    monkeypatch.setattr(session_authority, '_MARKER_RELEASE_WAIT_S', 0.05)
    release_first_reply = asyncio.Event()
    send = adapter.send

    async def slow_send(chat_id, content, reply_to=None, metadata=None):
        if content == 'reply to first':
            await release_first_reply.wait()
        return await send(chat_id, content, reply_to=reply_to, metadata=metadata)
    adapter.send = slow_send
    gates['first'] = asyncio.Event()
    await adapter.handle_message(MessageEvent(text='first', source=source, message_id='m1'))
    await _wait_for(lambda: _statuses(authority).get('first') == 'started')
    await asyncio.wait_for(adapter.handle_message(MessageEvent(text='follow-up', source=source, message_id='m2')), 5)
    gates['first'].set()
    await _wait_for(lambda: 'follow-up' in {text for text, _ in cleaned}, 15)
    assert 'first' not in {text for text, _ in cleaned}, "B's fire swept A's cards before A's reply"
    release_first_reply.set()
    await _wait_for(lambda: 'first' in {text for text, _ in cleaned}, 15)
    assert sorted(cleaned) == [('first', True), ('follow-up', True)]
    await _settled(authority, adapter, cleaned)
