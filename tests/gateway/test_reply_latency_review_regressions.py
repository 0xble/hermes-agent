"""Review regressions for durable receipt recovery, queued silence and shutdown (#1095)."""
import asyncio
import json
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import reply_latency as latency
from gateway.session_envelope import _adapter_event
from tests.gateway.test_reply_latency_logs import Adapter, event, records


def _restored_head():
    adapter = Adapter()
    head, follower = event(adapter, 'head'), event(adapter, 'human-follower')
    head._attended_activity_received_at = 300
    head._attended_activity_marked = True
    follower._attended_activity_received_at = 250
    follower._attended_activity_marked = True
    head._hermes_absorbed_events = [follower]
    original = head._hermes_absorbed_events
    latency.restore_inputs(head, [
        {'message_id': 'head', 'received_at': 100},
        {'message_id': 'old-merged-input', 'received_at': 200},
    ])
    return head, original, follower


def test_restored_latency_inputs_leave_real_absorbed_provenance_untouched():
    head, original, follower = _restored_head()
    assert head._hermes_absorbed_events is original
    assert len(head._hermes_absorbed_events) == 1
    assert head._hermes_absorbed_events[0] is follower
    latency.bind_agent_start(head)
    assert head._hermes_absorbed_events is original
    assert head._hermes_absorbed_events[0] is follower


def test_restored_latency_inputs_do_not_reopen_marked_attended_activity():
    head, _, _ = _restored_head()
    projected = _adapter_event(head)
    assert projected._attended_activity_received_at == 300
    assert projected._attended_activity_marked is True
    assert head._attended_activity_received_at == 300
    assert head._attended_activity_marked is True


def test_private_restored_receipts_settle_once_across_rebind(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    caplog.set_level('INFO', logger='gateway.reply_latency')
    head = event(Adapter(), 'head')
    descriptors = [{'message_id': 'head', 'received_at': 100},
                   {'message_id': None, 'received_at': 200}]
    latency.restore_inputs(head, descriptors)
    restored_id = head._reply_latency_inputs[1]['receipt_id']
    assert 'receipt_id' not in descriptors[1]  # caller-owned spool data is unchanged
    for _ in range(2):
        latency.bind_agent_start(head)
        receipt = latency.current_receipt()
        assert len(receipt.absorbed) == 1
        assert receipt.absorbed[0].identity['receipt_id'] == restored_id
        latency.record_no_reply(receipt)
    assert [row['message_id'] for row in records(caplog) if row['event'] == 'no_reply'] == ['head', None]
    assert not getattr(head, '_hermes_absorbed_events', ())


@pytest.mark.asyncio
@pytest.mark.parametrize('token', ['NO_REPLY', '[SILENT]'])
async def test_queued_silent_head_and_visible_followup_settle_independently(monkeypatch, tmp_path, caplog, token):
    from tests.gateway.test_gateway_silence_tokens import _runner, _event
    caplog.set_level('INFO', logger='gateway.reply_latency')
    runner = _runner(monkeypatch, tmp_path)
    runner._MAX_INTERRUPT_DEPTH = 8
    runner._session_key_for_source = lambda source: 'route'
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value='follow-up')
    runner._delivery_adapter_for = lambda source: None
    runner._intake_adapter_for = lambda source: None
    runner._pinned_channel_inputs = lambda key, prompt, source, **kwargs: (prompt, source)
    runner._persist_prompt_pins = AsyncMock()
    runner._refresh_agent_cache_message_count = AsyncMock()
    head, followup = _event(reply_expected=False), _event(reply_expected=True)
    head.message_id, followup.message_id = 'silent-head', 'visible-followup'
    latency.bind_agent_start(head)
    head_receipt = latency.current_receipt()

    async def execute_followup(**kwargs):
        latency.record_first_reply(latency.current_receipt(), 'visible answer')
        return {'final_response': 'visible answer', 'messages': [], 'failed': False}

    runner._run_agent = AsyncMock(side_effect=execute_followup)
    ctx = SimpleNamespace(
        source=head.source, session_id='sid', session_key='route', run_generation=1,
        _interrupt_depth=0, history=[], _status_thread_metadata=None, context_prompt=None,
        result_holder=[None], stream_consumer_holder=[None], mute_notification_reply=False,
        persist_user_display_kind=None, reply_expected=False, _post_delivery_adapter=None,
    )
    result = {'final_response': token, 'failed': False, 'messages': []}
    await runner._run_agent_queued_followup(ctx, None, 'follow-up', followup, result, result, None)
    terminal = [(row['message_id'], row['event']) for row in records(caplog)
                if row['event'] in ('first_reply', 'no_reply')]
    assert terminal == [('silent-head', 'no_reply'), ('visible-followup', 'first_reply')]
    assert latency.current_receipt() is head_receipt
    assert head_receipt.no_reply and not head_receipt.replied
    assert followup._reply_latency_receipt.replied and not followup._reply_latency_receipt.no_reply


@pytest.mark.asyncio
@pytest.mark.parametrize('token', ['NO_REPLY', '[SILENT]'])
async def test_real_queued_chain_silent_head_has_its_own_terminal_receipt(monkeypatch, tmp_path, caplog, token):
    from gateway.config import GatewayConfig
    from gateway.platforms.base import MessageEvent
    from tests.gateway.test_queued_followup_processing_hooks import (
        SESSION_KEY, HookRecordingAdapter, _TwoTurnAgent, _install_fake_agent, _make_runner, _source,
    )

    adapter = HookRecordingAdapter()
    followup = MessageEvent(text='follow-up', source=_source(), message_id='chain-followup')

    class SilentHeadAgent(_TwoTurnAgent):
        calls = []

        def run_conversation(self, *args, **kwargs):
            result = super().run_conversation(*args, **kwargs)
            if len(self.calls) == 1:
                adapter._pending_messages[SESSION_KEY] = followup
                result['final_response'] = token
            return result

    _install_fake_agent(monkeypatch, tmp_path, SilentHeadAgent)
    runner = _make_runner(adapter)
    runner.config.streaming = GatewayConfig().streaming
    runner.config.streaming.enabled = False
    head = MessageEvent(text='head', source=_source(), message_id='chain-head', reply_expected=False)
    with caplog.at_level('INFO', logger='gateway.reply_latency'):
        latency.bind_agent_start(head)
        result = await runner._run_agent(
            message=head.text, context_prompt='', history=[], source=head.source,
            session_id='silent-chain', session_key=SESSION_KEY, inbound_message_id=head.message_id,
            reply_expected=False,
        )
        assert result['queued_terminal_reply_receipt'] is followup._reply_latency_receipt
        latency.record_first_reply(result['queued_terminal_reply_receipt'], result['final_response'])
    terminal = [(row['message_id'], row['event']) for row in records(caplog)
                if row['event'] in ('first_reply', 'no_reply')]
    assert terminal == [('chain-head', 'no_reply'), ('chain-followup', 'first_reply')]


@pytest.mark.asyncio
@pytest.mark.parametrize(('restart', 'elapsed', 'expected'), [(False, 2.75, 2.0), (True, 2.75, 0.25), (True, 4.0, 0.0)])
async def test_shutdown_evidence_drain_uses_remaining_restart_budget(monkeypatch, restart, elapsed, expected):
    import gateway.run_shutdown as shutdown
    from gateway.run import GatewayRunner
    clock = [100.0]
    monkeypatch.setattr(shutdown.time, 'monotonic', lambda: clock[0])

    async def final_kill(*args, **kwargs):
        clock[0] += elapsed

    monkeypatch.setattr(GatewayRunner, '_stop_kill_tool_subprocesses_off_loop', final_kill)
    monkeypatch.setattr('agent.auxiliary_client.shutdown_cached_clients', lambda: None)
    budgets = []
    monkeypatch.setattr('gateway.reply_latency_sink.shutdown_sinks', lambda timeout, **kwargs: budgets.append(timeout))
    runner = SimpleNamespace(
        _background_tasks=set(), _stop_task=None, _restart_task=None, adapters={},
        _running_agents={}, _running_agents_ts={}, _pending_messages={}, _pending_approvals={},
        _shutdown_event=asyncio.Event(), _restart_requested=restart,
        _active_api_run_count=lambda: 0, _restart_shutdown_bound=lambda: 3.0,
    )
    try:
        await shutdown.GatewayShutdownMixin._stop_release_runtime_state(runner, SimpleNamespace(elapsed=lambda: elapsed))
    finally:
        # Restore before the autouse writer-drain fixture (including on a RED assertion).
        monkeypatch.undo()
    # An exhausted budget skips executor admission altogether.
    assert budgets == ([expected] if expected else [])
    assert runner._shutdown_event.is_set()


@pytest.mark.asyncio
async def test_restart_does_not_wait_for_executor_admission(monkeypatch):
    import gateway.run_shutdown as shutdown
    from gateway.run import GatewayRunner
    from gateway.reply_latency_sink import shutdown_sinks

    async def final_kill(*args, **kwargs):
        pass

    monkeypatch.setattr(GatewayRunner, '_stop_kill_tool_subprocesses_off_loop', final_kill)
    monkeypatch.setattr('agent.auxiliary_client.shutdown_cached_clients', lambda: None)
    release = asyncio.Event()
    real_to_thread = asyncio.to_thread

    async def queued_worker(func, *args, **kwargs):
        if func is shutdown_sinks:
            await release.wait()  # A saturated default executor has not admitted this work.
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(shutdown.asyncio, 'to_thread', queued_worker)
    runner = SimpleNamespace(
        _background_tasks=set(), _stop_task=None, _restart_task=None, adapters={},
        _running_agents={}, _running_agents_ts={}, _pending_messages={}, _pending_approvals={},
        _shutdown_event=asyncio.Event(), _restart_requested=True,
        _active_api_run_count=lambda: 0, _restart_shutdown_bound=lambda: 0.01,
    )
    task = asyncio.create_task(shutdown.GatewayShutdownMixin._stop_release_runtime_state(
        runner, SimpleNamespace(elapsed=lambda: 0)))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    finally:
        release.set()
        await task


def test_late_shutdown_worker_cannot_restart_its_wait_budget(monkeypatch):
    import threading
    import time
    from gateway import reply_latency_sink as sinks
    waits = []
    fake = SimpleNamespace(enqueue_lock=threading.Lock(), closed=threading.Event(),
                           stop=lambda timeout: waits.append(timeout) or True)
    monkeypatch.setattr(sinks, '_instances', [fake])
    try:
        assert sinks.shutdown_sinks(2.0, deadline=time.monotonic() - 1.0)
    finally:
        monkeypatch.undo()
    assert fake.closed.is_set()
    assert waits == [0.0]


def test_sink_recovers_same_day_after_evidence_directory_disappears(tmp_path):
    from gateway.reply_latency_sink import ReplyLatencySink
    sink = ReplyLatencySink(tmp_path)
    try:
        sink.checkpoint()
        sink.flush()
        shutil.rmtree(sink.root)
        sink.write({'event': 'received', 'profile': 'default', 'timestamp': 100, 'message_id': 'after-removal'})
        sink.flush()
        rows = [json.loads(line) for path in sink.root.glob('*.jsonl') for line in path.read_text().splitlines()]
        assert [row['message_id'] for row in rows if row['event'] == 'received'] == ['after-removal']
        # Recreated evidence cannot attest to coverage before the directory was lost.
        sink.checkpoint()
        sink.flush()
        rows = [json.loads(line) for path in sink.root.glob('*.jsonl') for line in path.read_text().splitlines()]
        starts = [row for row in rows if row['event'] == 'instrumentation_start']
        assert len(starts) == 1
        coverage = [row for row in rows if row['event'] == 'coverage'][-1]
        assert coverage['coverage_start'] >= starts[0]['timestamp']
    finally:
        assert sink.stop()
