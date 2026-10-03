"""Forward-only config drives real PTB controlled polling and transfer receipts."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
pytest.importorskip('telegram')
from telegram.request import BaseRequest

from gateway.config import PlatformConfig
from gateway.generation import GenerationCoordinator, GenerationIdentity
from gateway.run_generation import ActiveGeneration
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.polling_transfer import ControlledPoller, PollingJournal


class Request(BaseRequest):
    def __init__(self):
        self.calls = 0
        self.finish = asyncio.Event()
        self.requested = asyncio.Event()

    @property
    def read_timeout(self):
        return 10

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        if url.endswith('/getMe'):
            return 200, b'{"ok":true,"result":{"id":1,"is_bot":true,"first_name":"Stub","username":"stub_bot"}}'
        if url.endswith('/getUpdates'):
            self.calls += 1
            if self.calls > 1:
                self.requested.set()
                await self.finish.wait()
            return 200, b'{"ok":true,"result":[]}'
        return 200, b'{"ok":true,"result":true}'


@pytest.mark.asyncio
async def test_forward_only_connect_and_transfer_persist_stop_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_GATEWAY_LOCK_DIR', str(tmp_path / 'locks'))
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: 123)
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture')
    (tmp_path / 'config.yaml').write_text('gateway:\n  forward_only_handover:\n    enabled: true\n', encoding='utf-8')
    db = GenerationCoordinator(tmp_path)
    owner = GenerationIdentity.create(release_sha='r', label='r', start_fingerprint=f'{os.getpid()}:123')
    db.register(owner, state='serving')
    epoch = db.acquire_lease('active_generation', owner.id)
    next_owner = db.reserve_generation(release_sha='next', label='next')
    db.claim_generation(next_owner.id, os.getpid()+1, 'next', boot_id='fixture')
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123456:CONTROLLED_STUB', extra={
        'drop_pending_on_cold_boot': False}))
    request = Request()
    monkeypatch.setattr(adapter, '_build_ptb_requests', AsyncMock(return_value=(Request(), adapter._instrument_polling_request(request))))
    monkeypatch.setattr(adapter, '_start_post_connect_housekeeping', lambda: None)
    active = ActiveGeneration(tmp_path, db, owner, epoch)
    active.runner = SimpleNamespace(adapters={'telegram': adapter}, config=SimpleNamespace(), _overlap_draining=False)
    # The drain task follows receipt persistence and needs the full live runner.
    monkeypatch.setattr(active, '_drain_after_transfer', AsyncMock())
    try:
        assert await adapter.connect()
        assert isinstance(adapter._controlled_journal, PollingJournal)
        assert isinstance(adapter._controlled_poller, ControlledPoller)
        await asyncio.wait_for(request.requested.wait(), 2)
        roster = active._telegram_adapters()
        assert len(roster) == 1
        db.request_transfer(owner.id, next_owner.id, epoch, set(roster))
        stopping = asyncio.create_task(active.transfer_requested(next_owner.id))
        request.finish.set()
        result = await asyncio.wait_for(stopping, 3)
        assert result['poller_stopped'] and result['tokens'] == 1
        receipts = db.transfer_receipts(owner.id, epoch)
        assert len(receipts) == 1 and receipts[0]['poller_stopped'] == 1
        assert receipts[0]['safe_offset'] == adapter._controlled_journal.safe_offset()
        assert [row['event'] for row in db.poller_journal()] == [
            'lock_acquired', 'poller_started', 'poller_stopped', 'lock_released']
        assert db.check_poller_journal()['ok']
    finally:
        request.finish.set()
        await adapter.disconnect()
        if active._drain_task is not None:
            await active._drain_task
