"""Exercise the installed readers against real gateway observations."""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway import reply_latency as latency
from tests.gateway.test_reply_latency_logs import Adapter, event


def reader(name):
    root = Path('/Users/brianle/Repos/scripts')
    if not root.is_dir():
        pytest.skip('consumer integration requires /Users/brianle/Repos/scripts checkout')
    path = root / 'src' / 'hermes' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, 'dont_write_bytecode', True):
        spec.loader.exec_module(module)
    return module


def sink_rows(home=None):
    home = Path(home or os.environ['HERMES_HOME'])
    return [json.loads(line) for path in sorted((home / 'logs/reply-latency').glob('*.jsonl'))
            for line in path.read_text().splitlines()]


@pytest.mark.asyncio
async def test_gateway_log_remains_accepted_by_external_text_reader(caplog, tmp_path):
    consumer = reader('report-reply-latency')
    caplog.set_level('INFO', logger='gateway.reply_latency')
    adapter = Adapter()
    incoming = event(adapter, 'inbound')
    latency.record_admitted(incoming)
    latency.bind_agent_start(incoming)
    receipt = latency.current_receipt()
    latency.record_first_reply(receipt, 'Working', kind='progress_send')
    await adapter.send_final_ledgered(incoming, 'route', 'Answer', {}, reply_to=None)
    lines = [r.message for r in caplog.records if r.message.startswith(consumer.MARKER)]
    parsed = [consumer.parse_record(line.partition(consumer.MARKER)[2]) for line in lines]
    assert [r['event'] for r in parsed] == ['received', 'agent_start', 'first_reply']
    assert all(r.get('delivery_kind', 'text_send').startswith('text_') for r in parsed)
    log = tmp_path / 'gateway.log'
    log.write_text('\n'.join(lines))
    assert consumer.main(['--json', str(log)]) == 0
    assert {'admitted', 'first_visible', 'final_delivery'} <= {r['event'] for r in sink_rows()}
    assert [r['delivery_kind'] for r in sink_rows() if r['event'] == 'first_reply'] == ['text_send']


@pytest.mark.asyncio
async def test_factory_reader_accepts_writer_and_proves_window_coverage(monkeypatch):
    consumer = reader('factory_kpis')
    clock = [1800000000.0]
    monkeypatch.setattr(latency, 'time', SimpleNamespace(time=lambda: clock[0]))
    adapter = Adapter()
    incoming = event(adapter, 'inbound')
    latency.record_admitted(incoming)
    latency.bind_agent_start(incoming)
    clock[0] += 2
    latency.record_first_reply(latency.current_receipt(), 'Working', kind='progress_send')
    clock[0] += 3
    await adapter.send_final_ledgered(incoming, 'route', 'Answer', {}, reply_to=None)
    # Rehydrating the same logical input must not change its cohort.
    clock[0] += 1
    latency.record_received(event(adapter, 'inbound'))
    rows = sink_rows()
    required = {'schema', 'process_id', 'profile', 'sequence', 'observed_at', 'started_at'}
    assert all(required <= row.keys() for row in rows)
    assert len({r['process_id'] for r in rows}) == 1
    assert [r['sequence'] for r in rows] == list(range(1, len(rows) + 1))
    assert len([r for r in rows if r['event'] == 'instrumentation_start']) == 1
    assert all(r['inbound_identity'] == ['default', 'telegram', 'chat', None, 'inbound']
               for r in rows if r['event'] == 'received')
    # An idle checkpoint extends coverage without inventing a reply.
    clock[0] += 60
    latency.record_coverage()
    evidence = consumer.reply_evidence(Path(os.environ['HERMES_HOME']) / 'logs',
                                       1800000000, clock[0], clock[0])
    assert evidence['errors'] == []
    assert evidence['coverage'] is True
    assert list(evidence['replies'].values()) == [{'received': 1800000000.0,
                                                 'first_reply': 1800000005.0}]


def test_idless_receipts_are_unique_and_factory_can_pair_each_reply():
    from gateway.session_envelope import snapshot_native, restore_native

    consumer = reader('factory_kpis')
    adapter = Adapter()
    codec = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=False),
        session_store=SimpleNamespace(_generate_session_key=lambda source: 'route'),
        _adapter_for_source=lambda source: adapter, _adapter_flag=lambda *args: False,
        _adapter_profile_for_source=lambda source: None,
        _is_user_authorized_for_source=lambda source, **kwargs: True,
        _delivery_adapter_for=lambda source: adapter,
    )
    replies = {}
    for _ in range(2):
        incoming = event(adapter, None)
        original = latency.record_received(incoming)
        restored = restore_native(snapshot_native(codec, incoming), codec)
        receipt = latency.record_received(restored)
        assert receipt.identity['receipt_id'] == original.identity['receipt_id']
        latency.record_final_reply(receipt, 'Answer', SimpleNamespace(success=True))
    for row in sink_rows():
        consumer.merge_reply(replies, row)
    assert len(replies) == 2
    assert all({'received', 'first_reply'} <= row.keys() for row in replies.values())


def test_process_sequence_spans_profiles_without_merging_their_inputs():
    from gateway.reply_latency_report import read_records

    for profile in ('default', 'secondary'):
        incoming = event(Adapter(), 'same-message')
        incoming.source.profile = profile
        latency.record_received(incoming)
    rows = sink_rows()
    assert len({r['process_id'] for r in rows}) == 1
    assert [r['sequence'] for r in rows] == list(range(1, len(rows) + 1))
    assert {row['profile'] for row in read_records()} == {'default', 'secondary'}


def test_deleted_sink_directory_is_recreated():
    incoming = event(Adapter(), 'inbound')
    receipt = latency.record_received(incoming)
    for path in receipt.log_dir.iterdir():
        path.unlink()
    receipt.log_dir.rmdir()
    latency.bind_agent_start(incoming)
    assert any(row['event'] == 'agent_start' for row in sink_rows())


@pytest.mark.asyncio
async def test_failed_native_finalize_rolls_back_without_final_observation():
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    from tests.gateway.test_stream_consumer_wecom_native import _make_native_streaming_adapter

    latency.bind_agent_start(event(Adapter(), 'inbound'))
    adapter = _make_native_streaming_adapter(finalize_succeeds=False)
    stream = GatewayStreamConsumer(adapter, 'chat', StreamConsumerConfig(cursor=''))
    stream._accumulated = 'Answer'
    assert await stream._native_push('Answer', finalize=False, is_turn_final=False)
    assert await stream._native_push('Answer', finalize=True, is_turn_final=True) is None
    assert not stream._final_content_delivered
    assert not [r for r in sink_rows() if r['event'] == 'final_delivery']


def test_report_skips_bad_utf8_and_unreadable_files_with_counts(tmp_path, caplog, monkeypatch):
    from gateway.reply_latency_report import read_records

    row = dict(platform='telegram', chat_id='c', thread_id=None, message_id='m',
               event='received', timestamp=100)
    good = json.dumps(row).encode() + b'\n'
    tail = json.dumps(dict(row, message_id='tail')).encode() + b'\n'
    (tmp_path / '2026-11-07.jsonl').write_bytes(good + b'\xff\n' + tail + b'{partial\n')
    unreadable = tmp_path / '2026-11-08.jsonl'
    unreadable.mkdir()
    assert {r['message_id'] for r in read_records(tmp_path)} == {'m', 'tail'}
    assert '2 invalid lines' in caplog.text
    assert '1 unreadable files' in caplog.text


@pytest.mark.asyncio
async def test_worker_status_mixin_scopes_confirmed_progress(monkeypatch):
    from gateway.run_turn_progress import GatewayTurnProgressMixin
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    adapter = Adapter()
    latency.bind_agent_start(event(adapter, 'inbound'))
    receipt = latency.current_receipt()
    ctx = TurnContext(source=event(adapter, 'source').source, _status_adapter=adapter,
                      _status_chat_id='chat', _status_thread_metadata={},
                      _run_still_current=lambda: True, _cleanup_progress=False)
    runner = TurnRunner(SimpleNamespace(_delivery_adapter_for=lambda source: adapter), ctx)
    scheduled = []
    monkeypatch.setattr(runner, '_schedule', lambda coro, *args: scheduled.append(coro))
    seen = []
    original = adapter.send

    async def send(chat_id, content, **kwargs):
        seen.append(latency.current_receipt())
        return latency.transport_confirmed(content, await original(chat_id, content, **kwargs),
                                           chat_id=chat_id)

    monkeypatch.setattr(adapter, 'send', send)
    await asyncio.to_thread(GatewayTurnProgressMixin._status_callback_sync, runner, 'lifecycle', 'Working')
    latency.restore_receipt(None)
    for coro in scheduled:
        await coro
    assert seen == [receipt]
    assert [r['delivery_kind'] for r in sink_rows() if r['event'] == 'first_visible'] == ['progress_send']


@pytest.mark.parametrize('scope', ['context', 'process'])
def test_receipt_sink_uses_isolated_home_even_inside_foreign_profile_scope(tmp_path, scope):
    from hermes_constants import (pin_process_hermes_home, reset_hermes_home_override,
                                  set_hermes_home_override)

    isolated = Path(os.environ['HERMES_HOME'])
    foreign = tmp_path / 'foreign-profile'
    token = set_hermes_home_override(str(foreign))
    if scope == 'process':
        pin_process_hermes_home(foreign)
    try:
        receipt = latency.record_received(event(Adapter(), 'inbound'))
    finally:
        reset_hermes_home_override(token)
        if scope == 'process':
            pin_process_hermes_home(None)
    assert receipt.log_dir == isolated / 'logs/reply-latency'
    assert not foreign.exists()
    assert any(r['event'] == 'received' for r in sink_rows(isolated))


@pytest.mark.asyncio
async def test_intentional_human_silence_is_recorded_for_factory_reader(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from tests.gateway.test_gateway_silence_tokens import _event, _runner

    runner = _runner(monkeypatch, tmp_path)
    incoming = _event(reply_expected=False)
    receipt = latency.record_received(incoming)
    runner._run_agent = AsyncMock(return_value={
        'final_response': '[SILENT]', 'messages': [], 'tools': [], 'history_offset': 0,
        'last_prompt_tokens': 0, 'api_calls': 1, 'failed': False,
    })
    assert await runner._handle_message_with_agent(incoming, incoming.source, 'route', 1) == ''
    rows = [r for r in sink_rows() if r['event'] == 'no_reply']
    assert len(rows) == 1
    assert rows[0]['message_id'] == receipt.identity['message_id']
    consumer = reader('factory_kpis')
    replies = {}
    for row in sink_rows():
        consumer.merge_reply(replies, row)
    assert len(replies) == 1
    assert 'no_reply' in next(iter(replies.values()))
