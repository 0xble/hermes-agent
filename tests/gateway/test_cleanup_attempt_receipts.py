"""Real Telegram gate/owner integration: local deferral is not an API attempt."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from gateway.config import PlatformConfig
from gateway.delegation_cards import DelegationCards
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
async def test_queued_delegation_cleanup_keeps_attempt_after_final_takes_priority(tmp_path):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='fake'))
    adapter._bot = MagicMock()
    adapter._bot.delete_message = AsyncMock(return_value=True)
    adapter._send_cooldown_max_wait = 0.3
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    item = {'source': {'platform': 'telegram', 'chat_id': '42'},
            'message_id': '7', 'retired': True, 'rows': {}, 'generation': 1,
            'owner': {'profile': 'default'}, 'started_at': time.time()}
    manager = DelegationCards(runner, home=tmp_path, interval=0)
    manager.cards['record'] = item
    pending = manager.pending
    lock = adapter._send_cooldown_lock('42')
    await lock.acquire()
    deleting = asyncio.create_task(manager._delete('record', item))
    try:
        async def queued():
            while not item.get('delete_attempts'):
                await asyncio.sleep(0)
        await asyncio.wait_for(queued(), 1)
        # Arrives after the owner's preflight, while the real per-chat lock is held.
        adapter._send_final_waiters['42'] = 1
        assert adapter.deletion_retry_after('42') >= 1.0
        lock.release()
        await deleting
        adapter._bot.delete_message.assert_not_awaited()
        assert item['delete_attempts'] == 0
        assert item['message_id'] == '7'
        assert item['delete_retry_at'] - time.time() > 0.8
        adapter._send_final_waiters.clear()
        async def drain():
            while pending:
                await asyncio.gather(*list(pending.values()))
        await asyncio.wait_for(drain(), 3)
        adapter._bot.delete_message.assert_awaited_once()
        assert item['delete_attempts'] == 1 and item['message_id'] is None
    finally:
        if lock.locked():
            lock.release()
        for task in list(pending.values()) + [deleting]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*list(pending.values()), deleting, return_exceptions=True)
