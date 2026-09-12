"""Actual update watcher/final delivery with durable files and fake transport."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.update_notifications import read_pending, save_pending
from tests.gateway.test_update_command import _make_runner
from tests.gateway.test_update_lifecycle_notifications import pending
from tests.gateway.update_fixtures import finalize_update
from tools.ansi_strip import strip_ansi


def setup_update(home, raw):
    pending(home)
    marker, data = read_pending(home)
    data['updating_notified'] = True
    save_pending(marker, data)
    (home / '.update_output.txt').write_bytes(raw)
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: SimpleNamespace(send=AsyncMock())}
    return runner, runner.adapters[Platform.TELEGRAM]


def body(text):
    return text[4:-4] if text.startswith('```\n') else None


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['result', 'exception'])
@pytest.mark.parametrize('restart', [False, True])
async def test_partial_chunks_retry_only_unacknowledged_suffix(tmp_path, failure, restart):
    raw = (' \n\x1b[31m' + 'é' * 3499 + ' 界 ' + 'z' * 3510 + '\x1b[0m\n\t').encode() + b'\xff'
    clean = strip_ansi(raw.decode('utf-8', errors='replace')).strip()
    runner, adapter = setup_update(tmp_path, raw)
    finalize_update(tmp_path)
    accepted, attempts = [], []
    failed = False

    async def send(chat, text, **kwargs):
        nonlocal failed
        if body(text) is not None:
            attempts.append(body(text))
            if len(attempts) == 2 and not failed:
                failed = True
                if failure == 'exception':
                    raise OSError('provider refused second chunk')
                return SimpleNamespace(success=False)
            accepted.append(body(text))
        return None  # Legacy adapter acceptance must remain compatible.

    adapter.send.side_effect = send
    with patch('gateway.run._hermes_home', tmp_path):
        assert await runner._send_update_notification() is False
        data = read_pending(tmp_path)[1]
        assert data.get('output_offset', 0) == 0
        assert data['output_batch']['ack'] == 3500
        if restart:
            runner = _make_runner()
            runner.adapters = {Platform.TELEGRAM: adapter}
        assert await runner._send_update_notification() is True
    assert ''.join(accepted) == clean
    assert accepted.count(clean[:3500]) == 1
    assert attempts[1] == attempts[2]
    assert read_pending(tmp_path) is None


@pytest.mark.asyncio
async def test_live_cancellation_then_startup_drains_frozen_batch_and_appended_output(tmp_path):
    runner, adapter = setup_update(tmp_path, b'A' * 3500 + b'B' * 500)
    blocked = asyncio.Event()
    accepted = []

    async def send(chat, text, **kwargs):
        payload = body(text)
        if payload and payload.startswith('B'):
            blocked.set()
            await asyncio.Future()
        if payload is not None:
            accepted.append(payload)
        return SimpleNamespace(success=True)

    adapter.send.side_effect = send
    with patch('gateway.run._hermes_home', tmp_path):
        task = asyncio.create_task(runner._watch_update_progress(poll_interval=.001, stream_interval=0))
        await asyncio.wait_for(blocked.wait(), 2)
        assert read_pending(tmp_path)[1]['output_batch']['ack'] == 3500
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with (tmp_path / '.update_output.txt').open('ab') as stream:
            stream.write(b'\nAPPENDED')
        finalize_update(tmp_path)
        replacement = _make_runner()
        replacement.adapters = {Platform.TELEGRAM: adapter}

        async def recovered(chat, text, **kwargs):
            if body(text) is not None:
                accepted.append(body(text))
            return SimpleNamespace(success=True)

        adapter.send.side_effect = recovered
        assert await replacement._send_update_notification() is False  # appended range remains
        assert read_pending(tmp_path)[1]['output_offset'] == 4000
        assert await replacement._send_update_notification() is True
    assert accepted == ['A' * 3500, 'B' * 500, 'APPENDED']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['truncate', 'same_size', 'replace_request', 'inherited_cursor'])
async def test_frozen_batch_or_request_mutation_cannot_advance_or_finish(tmp_path, mutation):
    runner, adapter = setup_update(tmp_path, b'A' * 3500 + b'B' * 500)
    finalize_update(tmp_path)
    adapter.send.side_effect = [SimpleNamespace(success=True), SimpleNamespace(success=False)]
    with patch('gateway.run._hermes_home', tmp_path):
        assert await runner._send_update_notification() is False
        checkpoint = read_pending(tmp_path)[1]['output_batch'].copy()
        if mutation == 'truncate':
            (tmp_path / '.update_output.txt').write_bytes(b'A')
        elif mutation == 'same_size':
            (tmp_path / '.update_output.txt').write_bytes(b'X' * 4000)
        elif mutation == 'inherited_cursor':
            marker, data = read_pending(tmp_path)
            data['reason'] = 'Replacement request carrying an old cursor'
            save_pending(marker, data)
            runner = _make_runner()
            runner.adapters = {Platform.TELEGRAM: adapter}
        else:
            # Replacement during a suspended transport call is fenced after the await.
            async def replace(chat, text, **kwargs):
                marker, data = read_pending(tmp_path)
                data['timestamp'] = '2099-01-01T00:00:00+00:00'
                data.pop('output_batch', None)
                save_pending(marker, data)
                return SimpleNamespace(success=True)
            adapter.send.side_effect = replace
        before = adapter.send.await_count
        assert await runner._send_update_notification() is False
        data = read_pending(tmp_path)[1]
        assert data.get('output_offset', 0) == 0
        if mutation != 'replace_request':
            assert data['output_batch'] == checkpoint
            assert adapter.send.await_count == before
        else:
            assert 'output_batch' not in data
            assert adapter.send.await_count == before + 1


@pytest.mark.asyncio
async def test_checkpoint_failure_stops_before_next_chunk(tmp_path):
    runner, adapter = setup_update(tmp_path, b'A' * 3500 + b'B' * 500)
    finalize_update(tmp_path)
    adapter.send.return_value = SimpleNamespace(success=True)
    from gateway import update_notifications
    real_save = update_notifications.save_pending

    def fail_ack(marker, data):
        if data.get('output_batch', {}).get('ack', 0):
            raise OSError('checkpoint unavailable')
        real_save(marker, data)

    with patch('gateway.run._hermes_home', tmp_path), patch.object(update_notifications, 'save_pending', fail_ack):
        assert await runner._send_update_notification() is False
    assert adapter.send.await_count == 1
    assert read_pending(tmp_path)[1]['output_batch']['ack'] == 0
    assert read_pending(tmp_path)[1].get('output_offset', 0) == 0


@pytest.mark.asyncio
async def test_live_and_final_drains_serialize_and_preserve_phase_checkpoint(tmp_path):
    runner, adapter = setup_update(tmp_path, b'A' * 3500 + b'B' * 500)
    entered, release = asyncio.Event(), asyncio.Event()
    accepted = []

    async def send(chat, text, **kwargs):
        payload = body(text)
        if text.startswith('✅'):
            assert read_pending(tmp_path)[1]['restarting_notified']
        if payload is not None:
            accepted.append(payload)
            if len(accepted) == 1:
                entered.set()
                await release.wait()
        return SimpleNamespace(success=True)

    adapter.send.side_effect = send
    with patch('gateway.run._hermes_home', tmp_path):
        watch = asyncio.create_task(runner._watch_update_progress(poll_interval=.001, stream_interval=0))
        await asyncio.wait_for(entered.wait(), 2)
        finalize_update(tmp_path)
        # The updater claims the same request while the first send is suspended.
        marker, _ = read_pending(tmp_path)
        marker.rename(tmp_path / '.update_pending.claimed.json')
        final = asyncio.create_task(runner._send_update_notification())
        assert await runner._send_update_phase('restarting')
        assert read_pending(tmp_path)[1]['restarting_notified']
        release.set()
        assert await asyncio.wait_for(final, 2)
        await asyncio.wait_for(watch, 2)
    assert accepted == ['A' * 3500, 'B' * 500]


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [b' \n\t', b'\x1b[31m\x1b[0m', b'\xff\xfe'])
async def test_empty_sanitized_and_invalid_utf8_batches_finish(tmp_path, raw):
    runner, adapter = setup_update(tmp_path, raw)
    finalize_update(tmp_path)
    adapter.send.return_value = SimpleNamespace(success=True)
    with patch('gateway.run._hermes_home', tmp_path):
        assert await runner._send_update_notification()
    output = [body(c.args[1]) for c in adapter.send.call_args_list if body(c.args[1]) is not None]
    expected = strip_ansi(raw.decode('utf-8', errors='replace')).strip()
    assert output == ([expected] if expected else [])


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['output', 'phase', 'final'])
async def test_replacement_during_send_never_acknowledges_or_removes_new_request(tmp_path, stage):
    runner, adapter = setup_update(tmp_path, b'old output' if stage == 'output' else b'')
    finalize_update(tmp_path)
    replacement = None

    async def replace(chat, text, **kwargs):
        nonlocal replacement
        marker, data = read_pending(tmp_path)
        data['timestamp'] = '2099-01-01T00:00:00+00:00'
        data.pop('output_batch', None)
        data.pop('output_offset', None)
        replacement = data.copy()
        save_pending(marker, data)
        return SimpleNamespace(success=True)

    adapter.send.side_effect = replace
    with patch('gateway.run._hermes_home', tmp_path):
        if stage == 'phase':
            assert await runner._send_update_phase('restarting') is False
        else:
            assert await runner._send_update_notification() is False
    assert adapter.send.await_count == 1
    assert read_pending(tmp_path)[1] == replacement
    assert (tmp_path / '.update_output.txt').exists()


@pytest.mark.asyncio
async def test_frozen_log_changed_during_send_does_not_checkpoint_acceptance(tmp_path):
    runner, adapter = setup_update(tmp_path, b'A' * 3500 + b'B' * 500)
    finalize_update(tmp_path)

    async def mutate(chat, text, **kwargs):
        (tmp_path / '.update_output.txt').write_bytes(b'X' * 4000)
        return SimpleNamespace(success=True)

    adapter.send.side_effect = mutate
    with patch('gateway.run._hermes_home', tmp_path):
        assert await runner._send_update_notification() is False
    assert adapter.send.await_count == 1
    data = read_pending(tmp_path)[1]
    assert data.get('output_offset', 0) == 0
    assert data['output_batch']['ack'] == 0
