"""A typed message's post-commit admission read never blocks the gateway event loop.

``_await_native_reply`` re-reads the committed admission row to learn whether a drain already
settled it. That read takes the SessionDB writer lock, which turn workers hold for seconds under
load. Done on the loop thread it froze every adapter: on 2026-11-27 a typed Telegram message
waited about 2 minutes behind other sessions' writes before its turn started.
"""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, SessionSource
from gateway.session_ingress import admit_message
from tests.waiting import async_wait_until


def _event(text='typed'):
    return MessageEvent(text=text, source=SessionSource(platform=Platform.TELEGRAM, chat_id='chat'))


def _authority():
    async def admit(event):
        return SimpleNamespace(status='queued', admission_id='a1', ref=None)
    return SimpleNamespace(admit_native=admit, native_waiters=set(), waiters={}, db=None)


@pytest.mark.asyncio
async def test_admission_read_waiting_on_the_writer_lock_runs_off_the_loop(monkeypatch):
    """The read can queue behind other sessions' writes for seconds: it must not hold the loop."""
    authority = _authority()
    loop_thread, read_threads = threading.get_ident(), []

    def read(db, admission_id):
        read_threads.append(threading.get_ident())
        return {'status': 'queued'}

    monkeypatch.setattr('hermes_state_runtime.get_session_admission', read)
    monkeypatch.setattr('gateway.session_ingress._claim_post_delivery', lambda *args: None)
    reply = asyncio.create_task(admit_message(authority, _event()))
    await async_wait_until(lambda: read_threads and 'a1' in authority.waiters, timeout=5)
    assert loop_thread not in read_threads, 'the admission read ran on the event-loop thread'
    authority.waiters['a1'].set_result('answer')
    assert await asyncio.wait_for(reply, 5) == 'answer'


@pytest.mark.asyncio
async def test_reply_handed_over_during_the_read_is_returned_not_dropped(monkeypatch):
    """The drain can settle the row and hand its reply to the waiter while the read is off the loop."""
    authority = _authority()
    loop = asyncio.get_running_loop()

    def read_after_settlement(db, admission_id):
        loop.call_soon_threadsafe(authority.waiters[admission_id].set_result, 'answer')
        return {'status': 'terminal', 'outcome': 'completed'}

    monkeypatch.setattr('hermes_state_runtime.get_session_admission', read_after_settlement)
    claimed = []
    monkeypatch.setattr('gateway.session_ingress._claim_post_delivery', lambda *args: claimed.append(args[1]))
    assert await asyncio.wait_for(admit_message(authority, _event()), 5) == 'answer'
    assert claimed == ['a1']


@pytest.mark.asyncio
async def test_row_settled_before_the_waiter_still_returns_nothing(monkeypatch):
    """A drain that settled the row before the waiter existed sends the reply itself."""
    authority = _authority()
    monkeypatch.setattr('hermes_state_runtime.get_session_admission',
                        lambda db, admission_id: {'status': 'terminal', 'outcome': 'completed'})
    pause = AsyncMock()
    monkeypatch.setattr('gateway.session_ingress._pause_reply', pause)
    assert await asyncio.wait_for(admit_message(authority, _event()), 5) is None
    assert 'a1' not in authority.waiters and 'a1' not in authority.native_waiters
    pause.assert_not_awaited()
