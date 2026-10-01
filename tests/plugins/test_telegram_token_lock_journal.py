"""Actual adapter token edges enclose the controlled poller's wire intervals."""
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip('telegram')
sys.path.insert(0, str(Path(__file__).resolve().parent))
from telegram_polling_stub import BotAPI
from gateway.config import PlatformConfig
from gateway.generation import GenerationCoordinator, GenerationIdentity
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['overlap_handover', 'forward_only_handover'])
async def test_real_adapter_journals_transfer_rearm_reconnect_disconnect(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_GATEWAY_LOCK_DIR', str(tmp_path / 'locks'))
    monkeypatch.setenv('HERMES_TELEGRAM_DISABLE_FALLBACK_IPS', '1')
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: 123)
    (tmp_path / 'config.yaml').write_text(f'gateway:\n  {mode}:\n    enabled: true\n', encoding='utf-8')
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture')
    coordinator = GenerationCoordinator(tmp_path)
    owner = GenerationIdentity.create(release_sha='r', label='r', start_fingerprint=f'{os.getpid()}:123')
    coordinator.register(owner, state='serving')
    coordinator.acquire_lease('active_generation', owner.id)
    api = BotAPI()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123456:LOCK_JOURNAL_STUB', extra={
        'base_url': api.url, 'base_file_url': api.url, 'drop_pending_on_cold_boot': False}))
    adapter._start_post_connect_housekeeping = lambda: None
    try:
        assert await adapter.connect()
        assert adapter._controlled_journal is not None
        assert adapter._controlled_poller is not None
        receipt = await adapter.stop_polling_for_transfer()
        assert isinstance(receipt['safe_offset'], int)
        assert coordinator.poller_journal()[-2]['event'] == 'poller_stopped'
        await adapter.disconnect()
        assert coordinator.check_poller_journal()['ok']
        # Pre-commit abort re-arms the same still-serving owner.
        assert await adapter.connect(polling_standby=True)
        await adapter.start_polling_from_transfer(receipt)
        await adapter.disconnect()
        assert coordinator.check_poller_journal()['ok']
        assert await adapter.connect(is_reconnect=True)
        await adapter.disconnect()
        assert coordinator.check_poller_journal()['ok']
        assert [row['event'] for row in coordinator.poller_journal()] == [
            'lock_acquired', 'poller_started', 'poller_stopped', 'lock_released'] * 3
    finally:
        await adapter.disconnect()
        api.close()


@pytest.mark.asyncio
async def test_slow_lock_evidence_preserves_order_without_stalling_wire(tmp_path, monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace
    from plugins.platforms.telegram.polling_transfer import ControlledPoller, PollingJournal

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_GATEWAY_LOCK_DIR', str(tmp_path / 'locks'))
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: 123)
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture')
    coordinator = GenerationCoordinator(tmp_path)
    owner = GenerationIdentity.create(release_sha='r', label='r', start_fingerprint=f'{os.getpid()}:123')
    coordinator.register(owner, state='serving')
    coordinator.acquire_lease('active_generation', owner.id)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123456:ORDERING_STUB'))
    journal = PollingJournal(coordinator, adapter.config.token)
    adapter._controlled_journal = journal
    entered, release_write = threading.Event(), threading.Event()
    requested, release_request = asyncio.Event(), asyncio.Event()
    original = journal.record_lifecycle

    def record(owner, event, **kwargs):
        if event == 'lock_acquired':
            entered.set()
            assert release_write.wait(5)
        original(owner, event, **kwargs)

    monkeypatch.setattr(journal, 'record_lifecycle', record)

    async def get_updates(**kwargs):
        requested.set()
        await release_request.wait()
        return []

    poller = None
    try:
        assert await adapter._acquire_polling_token_lock()
        assert await asyncio.to_thread(entered.wait, 2)
        poller = ControlledPoller(SimpleNamespace(bot=SimpleNamespace(get_updates=get_updates),
                                 update_queue=asyncio.Queue()), journal,
                                 lifecycle_predecessor=adapter._polling_lock_evidence)
        await asyncio.wait_for(poller.start(), 2)
        await asyncio.wait_for(requested.wait(), 2)
    finally:
        release_write.set()
        release_request.set()
        if poller is not None:
            assert (await poller.stop())['stopped']
        await adapter._release_polling_token_lock()
    assert coordinator.check_poller_journal()['ok']


@pytest.mark.asyncio
async def test_transfer_owner_lookup_failure_releases_lock_for_retry(tmp_path, monkeypatch):
    """A failed transfer owner lookup must not strand the token lock."""
    from types import SimpleNamespace
    from plugins.platforms.telegram.polling_transfer import PollingJournal

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    coordinator = GenerationCoordinator(tmp_path)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123456:TRANSFER_RETRY"))
    adapter._controlled_journal = PollingJournal(coordinator, adapter.config.token)
    adapter._app = SimpleNamespace(running=True)
    receipt = adapter._controlled_journal.stop_receipt()
    original_owner = adapter._controlled_journal.lifecycle_owner
    failed = True

    def fail_once():
        nonlocal failed
        if failed:
            failed = False
            raise RuntimeError("polling generation identity is not serving")
        return original_owner()

    monkeypatch.setattr(adapter._controlled_journal, "lifecycle_owner", fail_once)
    with pytest.raises(RuntimeError, match="old Telegram token holder"):
        await adapter.start_polling_from_transfer(receipt)
    assert not adapter.has_fatal_error
    assert adapter.fatal_error_retryable is True
    assert adapter._platform_lock_identity is None
    assert coordinator.poller_journal() == []

    assert await adapter._acquire_polling_token_lock()
    await adapter._release_polling_token_lock()
