"""A typed message's post-commit admission read never blocks the gateway event loop.

``_await_native_reply`` re-reads the committed admission row to learn whether a drain already
settled it. That read takes the SessionDB writer lock, which turn workers hold for seconds under
load. Done on the loop thread it froze every adapter: on 2026-11-27 a typed Telegram message
waited about 2 minutes behind other sessions' writes before its turn started.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, SessionSource
from gateway.session_ingress import admit_message


def _event(text='typed'):
    return MessageEvent(text=text, source=SessionSource(platform=Platform.TELEGRAM, chat_id='chat'))


def _authority():
    async def admit(event):
        return SimpleNamespace(status='queued', admission_id='a1', ref=None)
    return SimpleNamespace(admit_native=admit, native_waiters=set(), waiters={}, db=None)


async def _heartbeat(gaps, stop, tick=0.01):
    """Record gaps between ticks until *stop* is set: a frozen loop stops ticking."""
    last = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(tick)
        now = time.monotonic()
        gaps.append(now - last)
        last = now


@pytest.mark.asyncio
async def test_admission_read_waiting_on_the_writer_lock_keeps_the_loop_running(monkeypatch):
    authority = _authority()
    stall = 0.6  # the read queues behind another session's write

    def slow_read(db, admission_id):
        time.sleep(stall)
        return {'status': 'queued'}

    monkeypatch.setattr('hermes_state_runtime.get_session_admission', slow_read)
    monkeypatch.setattr('gateway.session_ingress._claim_post_delivery', lambda *args: None)
    gaps, stop = [], asyncio.Event()
    beat = asyncio.create_task(_heartbeat(gaps, stop))
    await asyncio.sleep(0.05)  # the heartbeat is ticking before the message arrives
    reply = asyncio.create_task(admit_message(authority, _event()))
    await asyncio.sleep(stall + 0.3)
    stop.set()
    await beat
    assert max(gaps) < stall / 2, f'the event loop froze for {max(gaps):.2f}s during the admission read'
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
