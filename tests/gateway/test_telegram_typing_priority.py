"""Typing is expendable at both topic egresses, without reserving a send gap."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_typing_yields_to_final_at_each_egress(monkeypatch, fallback):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fixture"))
    adapter._send_cooldown_seconds = 0
    lock = adapter._send_cooldown_lock("123")
    acquire = lock.acquire
    arrivals = asyncio.Queue()
    async def observed_acquire():
        arrivals.put_nowait(None)
        return await acquire()
    monkeypatch.setattr(lock, "acquire", observed_acquire)
    calls = []
    final_task = None
    async def final():
        calls.append("final")
    async def action(**kwargs):
        nonlocal final_task
        calls.append("topic-typing" if "message_thread_id" in kwargs else "typing")
        if fallback and "message_thread_id" in kwargs:
            final_task = asyncio.create_task(adapter._run_send_call("123", final))
            await asyncio.wait_for(arrivals.get(), 5)
            raise ValueError("message thread not found")
    adapter._bot = SimpleNamespace(send_chat_action=action)
    metadata = {"thread_id": "99", "telegram_dm_topic_reply_fallback": True}
    if fallback:
        # Ignore the first acquisition; observe the final queuing behind its transport.
        async def first_acquire():
            monkeypatch.setattr(lock, "acquire", observed_acquire)
            return await acquire()
        monkeypatch.setattr(lock, "acquire", first_acquire)
        await asyncio.wait_for(adapter.send_typing("123", metadata), 5)
    else:
        await acquire()
        typing_task = asyncio.create_task(adapter.send_typing("123", metadata))
        await asyncio.wait_for(arrivals.get(), 5)
        final_task = asyncio.create_task(adapter._run_send_call("123", final))
        await asyncio.wait_for(arrivals.get(), 5)
        lock.release()
        await asyncio.wait_for(typing_task, 5)
    await asyncio.wait_for(final_task, 5)
    assert calls == (["topic-typing", "final"] if fallback else ["final"])
    assert not adapter._send_final_waiters


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_uncontended_typing_does_not_reserve_real_send_gap(fallback):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fixture"))
    calls = []
    async def action(**kwargs):
        calls.append(kwargs)
        if fallback and "message_thread_id" in kwargs:
            raise ValueError("message thread not found")
    adapter._bot = SimpleNamespace(send_chat_action=action)
    await adapter.send_typing("123", {"thread_id": "99", "telegram_dm_topic_reply_fallback": True})
    assert len(calls) == (2 if fallback else 1)
    assert adapter._send_cooldown_until == {}
    assert not adapter._send_final_waiters
