"""An API conversation leaves the authority's memory between requests, as a restart leaves it.

Every API request binds its conversation (``bind_api_steps`` restores it from its binding receipt),
so a ``LiveSession`` kept after the FIFO goes idle only pinned its event stream and replay ring for
the life of the process. Retirement must never strand an admission committed around it.
"""
import asyncio
import threading

import pytest

from gateway.session_api_turn import admit_api_turn, observe_api_controls, observe_api_turn
from tests.waiting import async_wait_until


def _answer(owner, replies, check=None):
    async def handle(event):
        from gateway.session_results import execution_result
        if check is not None:
            check()
        replies.append(event.text)
        execution_result.get()['result'] = {'final_response': 'ok:' + event.text}
        return 'ok:' + event.text
    owner.runner._handle_message = handle


@pytest.mark.asyncio
async def test_api_session_leaves_memory_when_idle_and_its_next_request_restores_it(api, owner):
    replies = []
    _answer(owner, replies)
    first = admit_api_turn(api, session_id='conv', user_message='one', conversation_history=[])
    result, _ = await asyncio.wait_for(observe_api_turn(first), 10)
    assert result['final_response'] == 'ok:one'
    await async_wait_until(lambda: 'conv' not in owner.sessions, timeout=10, what='the idle API session to retire')
    second = admit_api_turn(api, session_id='conv', user_message='two', conversation_history=[])
    result, _ = await asyncio.wait_for(observe_api_turn(second), 10)
    assert result['final_response'] == 'ok:two' and replies == ['one', 'two']
    # An exact retry of the settled first request still answers from its receipt.
    retry = admit_api_turn(api, session_id='conv', user_message='one', conversation_history=[],
                           request_id=first[2]['request_id'])
    assert retry[2]['admission_id'] == first[2]['admission_id']
    assert (await asyncio.wait_for(observe_api_turn(retry), 10))[0]['final_response'] == 'ok:one'


@pytest.mark.asyncio
@pytest.mark.parametrize('controls', [True, False], ids=['approval-stream', 'plain'])
async def test_api_admission_committed_after_its_session_retired_runs_with_its_observers(api, owner, controls):
    """The session retires while a request's admission commits (between its bind and the commit):
    the request's control observer (a streaming door) and the drain must land on the same restored
    session, and the turn runs. The retirement is applied right after the commit, the state that
    window leaves."""
    import contextlib
    replies, seen = [], []
    owner.runner.adapters = {}  # a real runner's map (no LOCAL adapter: this profile serves API only)
    admitted = admit_api_turn(api, session_id='race', user_message='hello', conversation_history=[])
    del owner.sessions['race']  # retired in the window

    def observed():
        seen.append(len(owner.sessions['race'].event_stream.observers))
    _answer(owner, replies, observed)
    with observe_api_controls(admitted, lambda *_: None) if controls else contextlib.nullcontext():
        result, _ = await asyncio.wait_for(observe_api_turn(admitted), 10)
    assert result['final_response'] == 'ok:hello'
    assert seen == [int(controls)]  # the approval projection is attached to the session the turn runs on


@pytest.mark.asyncio
async def test_api_admission_wakes_a_drain_during_idle_retirement(api, owner, monkeypatch):
    from gateway import session_viewer_close

    loop = asyncio.get_running_loop()
    probed = asyncio.Event()
    release = threading.Event()
    original_busy = session_viewer_close._busy

    def pause_after_idle_probe(*args):
        busy = original_busy(*args)
        if not busy and not release.is_set():
            loop.call_soon_threadsafe(probed.set)
            assert release.wait(10), 'the idle probe was not released'
        return busy

    monkeypatch.setattr(session_viewer_close, '_busy', pause_after_idle_probe)
    replies, sessions = [], []
    _answer(owner, replies, lambda: sessions.append(owner.sessions.get('conv')))
    first = admit_api_turn(api, session_id='conv', user_message='one', conversation_history=[])
    live = owner.sessions['conv']
    assert (await asyncio.wait_for(observe_api_turn(first), 10))[0]['final_response'] == 'ok:one'
    drain = live.task
    try:
        await asyncio.wait_for(probed.wait(), 10)
        # The idle read already returned false, but the drain has not acted on it. The second
        # admission must wake this same drain, not let it retire the session on a stale read.
        _, ref, _ = admit_api_turn(api, session_id='conv', user_message='two', conversation_history=[])
        assert not drain.done()
        owner._schedule(ref)
        assert live.task is drain and live.rescan
    finally:
        release.set()
    await asyncio.wait_for(drain, 10)
    assert replies == ['one', 'two']
    assert all(session is live for session in sessions)
    assert 'conv' not in owner.sessions  # now the FIFO really is idle


@pytest.mark.asyncio
async def test_api_session_with_a_viewer_or_queued_input_stays(api, owner):
    """Retirement sheds only what nobody uses: a session a viewer subscribes to stays after idle."""
    replies = []
    _answer(owner, replies)
    admitted = admit_api_turn(api, session_id='watched', user_message='hi', conversation_history=[])
    owner.sessions['watched'].subscribers['sub'] = object()
    live = owner.sessions['watched']
    await asyncio.wait_for(observe_api_turn(admitted), 10)
    # The retirement check runs at the drain's idle exit, before its task finishes.
    await async_wait_until(lambda: live.task.done(), timeout=10, what='the drain to go idle')
    assert owner.sessions.get('watched') is live


@pytest.mark.asyncio
async def test_deleting_an_idle_api_session_evicts_its_cached_agent(api, owner):
    """A delete or prune of an API conversation retirement let go still drops its cached agent
    (transcript, client pool, memory provider), as it does for one in memory."""
    _answer(owner, [])
    evicted = []
    owner.runner._evict_cached_agent = lambda route, **kw: evicted.append(route)
    admitted = admit_api_turn(api, session_id='conv', user_message='one', conversation_history=[])
    route = owner.sessions['conv'].route
    await asyncio.wait_for(observe_api_turn(admitted), 10)
    await async_wait_until(lambda: 'conv' not in owner.sessions, timeout=10, what='the idle API session to retire')
    from gateway.session_mutations import retire_live_sessions
    retire_live_sessions(owner, ['conv'])  # the delete / prune primitive
    assert evicted == [route]
