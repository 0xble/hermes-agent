"""A turn the durable FIFO drain runs without a live adapter handler (a recovered turn, a loop
wakeup, a watch notification) still fires its post-delivery cleanup, so its temporary progress
bubbles do not stay in the chat. A live native waiter's adapter lifecycle keeps that job."""
import asyncio

import pytest

from gateway.session_contract import Submission


class _Owner:
    def __init__(self, log):
        self.log = log

    async def _fire_post_delivery_callback(self, session_key, generation):
        self.log.append(('cleanup', session_key, generation))


async def _run(tmp_path, monkeypatch, reply, native=False):
    from gateway import session_ingress
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.session_authority import LiveSession
    from gateway.session_contract import Principal, SessionRef
    from gateway.session_results import execution_result
    from tests.gateway.test_prompt_attachments import _authority as _store_authority

    log = []

    async def answer(event):
        execution_result.get()['post_delivery'] = (_Owner(log), 'k', 7)
        if isinstance(reply, BaseException):
            raise reply
        return reply
    authority = await _store_authority(tmp_path, monkeypatch, answer)
    authority.sessions['s'] = LiveSession(SessionSource(platform=Platform.TELEGRAM, chat_id='c'), 's')
    authority.runner._adapter_for_source = lambda source: object()

    async def deliver(adapter, event, session_key, response):
        log.append(('sent', response))
    monkeypatch.setattr(session_ingress, 'deliver_response', deliver)
    actor = Principal('human', 'p', frozenset({'session:submit'}), 't')
    if native:
        from gateway.platforms.event import MessageEvent

        async def admit_native(event):
            return await authority.submit(actor, Submission('m1', SessionRef('p', 's'), {'text': 'hi'}, 'queue'))
        monkeypatch.setattr(authority, 'admit_native', admit_native)
        await asyncio.wait_for(session_ingress.admit_message(
            authority, MessageEvent(text='hi', source=authority.sessions['s'].source)), 10)
    else:
        await authority.submit(actor, Submission('m1', SessionRef('p', 's'), {'text': 'hi'}, 'queue'))
    await asyncio.wait_for(authority.sessions['s'].task, 10)
    for _ in range(50):
        if getattr(session_ingress, '_post_delivery_tasks', ()):
            await asyncio.sleep(0.01)
    return log, authority


@pytest.mark.asyncio
async def test_no_waiter_reply_fires_cleanup_after_the_send(tmp_path, monkeypatch):
    log, authority = await _run(tmp_path, monkeypatch, 'model reply')
    assert log == [('sent', 'model reply'), ('cleanup', 'k', 7)]
    assert not getattr(authority, 'post_deliveries', {})


@pytest.mark.asyncio
async def test_silent_turn_fires_cleanup_without_a_send(tmp_path, monkeypatch):
    log, _ = await _run(tmp_path, monkeypatch, '')
    assert log == [('cleanup', 'k', 7)]


@pytest.mark.asyncio
async def test_turn_that_raised_still_fires_cleanup(tmp_path, monkeypatch):
    log, _ = await _run(tmp_path, monkeypatch, RuntimeError('boom'))
    assert ('cleanup', 'k', 7) in log


@pytest.mark.asyncio
async def test_live_native_waiter_keeps_the_cleanup(tmp_path, monkeypatch):
    log, _ = await _run(tmp_path, monkeypatch, 'model reply', native=True)
    assert ('cleanup', 'k', 7) not in log


def test_registered_bubble_cleanup_is_recorded_for_the_drain():
    from types import SimpleNamespace
    from gateway.run_turn import GatewayTurnMixin
    from gateway.session_results import execution_result

    registered = []
    owner = SimpleNamespace(register_post_delivery_callback=lambda key, cb, generation=None: registered.append((key, generation)))
    turn_ctx = SimpleNamespace(
        _cleanup_msg_ids=['m1'], session_key='k', _status_delivery=None, _bubble_cleanup_registered=False,
        _cleanup_progress=True, _post_delivery_adapter=None, source=SimpleNamespace(chat_id='c'), run_generation=7)
    captured = {}
    token = execution_result.set(captured)
    try:
        GatewayTurnMixin._run_agent_schedule_bubble_cleanup(object(), {'final_response': 'ok'}, owner, turn_ctx)
    finally:
        execution_result.reset(token)
    assert registered == [('k', 7)]
    assert captured['post_delivery'] == (owner, 'k', 7)


@pytest.mark.asyncio
async def test_stale_generation_callbacks_fire_with_the_next_one():
    from gateway.config import Platform, PlatformConfig
    from tests.gateway.test_post_delivery_callback_chaining import _MinAdapter
    adapter = _MinAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    fired = []
    for gen in (1, 2, 4):
        adapter.register_post_delivery_callback('s', lambda g=gen: fired.append(g), generation=gen)
    adapter.register_post_delivery_callback('other', lambda: fired.append('other'), generation=1)
    await adapter._fire_post_delivery_callback('s', 3)
    assert fired == [1, 2]
    assert set(adapter._post_delivery_callbacks_by_generation) == {('s', 4), ('other', 1)}
