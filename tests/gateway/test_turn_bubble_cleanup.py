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
        if text in gates:
            await gates[text].wait()

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


@pytest.mark.asyncio
async def test_every_turn_path_deletes_its_own_bubbles(monkeypatch):
    from gateway.platforms.event import MessageEvent
    from gateway.session_automation import admit_route_wake

    runner, authority, adapter, source, gates, cleaned = await _owner(monkeypatch)
    gates['first'] = asyncio.Event()
    # Adapter lifecycle: the first message, while the session is idle.
    await adapter.handle_message(MessageEvent(text='first', source=source, message_id='m1'))
    await _wait_for(lambda: _statuses(authority).get('first') == 'started')
    # Busy follow-ups: a user message and an internal wake sent while 'first' runs.
    await asyncio.wait_for(adapter.handle_message(MessageEvent(text='follow-up', source=source, message_id='m2')), 5)
    await asyncio.wait_for(adapter.handle_message(MessageEvent(
        text='busy wake', source=source, message_id='m3', internal=True)), 5)
    gates['first'].set()
    await _wait_for(lambda: {'first', 'follow-up', 'busy wake'} <= {text for text, _ in cleaned}, 15)
    # FIFO drain with no live waiter: a producer wake (/loop tick, watch notification), delivered
    # by the drain like a recovered turn.
    assert await admit_route_wake(runner, adapter, source, 'loop tick', 'loop')
    await _wait_for(lambda: 'loop tick' in {text for text, _ in cleaned}, 15)
    await _wait_for(lambda: not adapter._background_tasks, 15)
    assert sorted(cleaned) == sorted([
        ('first', True), ('follow-up', True), ('busy wake', True), ('loop tick', True)])
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
