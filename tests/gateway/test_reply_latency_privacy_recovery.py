"""Privacy and shutdown-spool provenance survive the gateway's event copies."""
from types import SimpleNamespace

import pytest

from agent.incognito import incognito_scope
from gateway import reply_latency as latency
from gateway.run_pending_recovery import _recovered_followup_event
from gateway.session_envelope import _adapter_event
from gateway.shutdown_flush import _serialise_value
from tests.gateway.test_reply_latency_logs import Adapter, event


def test_incognito_receipts_never_reach_the_durable_sink(tmp_path, monkeypatch):
    from gateway.reply_latency_sink import sink_for_home
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    incoming = event(Adapter(), 'private')
    with incognito_scope(True, 'private-session'):
        latency.bind_agent_start(incoming)
        receipt = latency.current_receipt()
    # Delivery can outlive the scope that created its receipt.
    latency.record_first_reply(receipt, 'private answer', SimpleNamespace(success=True))
    sink_for_home(tmp_path).flush()
    assert not list((tmp_path / 'logs/reply-latency').glob('*.jsonl'))


def test_public_receipt_emit_in_private_context_is_not_persisted(tmp_path, monkeypatch):
    from gateway.reply_latency_sink import sink_for_home
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    incoming = event(Adapter(), 'public')
    receipt = latency.record_received(incoming)
    sink_for_home(tmp_path).flush()
    before = [p.read_bytes() for p in (tmp_path / 'logs/reply-latency').glob('*.jsonl')]
    with incognito_scope(True, 'private-session'):
        latency.record_first_reply(receipt, 'private answer')
    sink_for_home(tmp_path).flush()
    assert [p.read_bytes() for p in (tmp_path / 'logs/reply-latency').glob('*.jsonl')] == before


def test_idless_receipt_and_clock_survive_shutdown_spool(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    original = event(Adapter(), None)
    original._attended_activity_received_at = 100
    first = latency.record_received(original)
    data = _serialise_value(original)
    assert data['_reply_latency_receipt_id'] == first.identity['receipt_id']
    latency._live.clear()  # recovery in a new process must not need live objects
    restored = _recovered_followup_event(data, original.source, 'human')
    second = latency.record_received(restored)
    assert second.identity['receipt_id'] == first.identity['receipt_id']
    assert latency.stamp_received(restored) == 100


def test_projected_reply_uuid_keeps_its_original_clock(tmp_path, monkeypatch):
    from gateway.platforms.base import merge_pending_message_event
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    adapter = Adapter()
    first, second = event(adapter, None), event(adapter, None)
    first._attended_activity_received_at = 100
    second._attended_activity_received_at = 110
    latency.record_received(first)
    latency.record_received(second)
    pending = {'route': first}
    merge_pending_message_event(pending, 'route', second, merge_text=True)
    projected = _adapter_event(first, text='merged')
    assert projected._reply_latency_receipt_id == first._reply_latency_receipt_id
    assert latency.stamp_received(projected) == 100
    # Attended routing independently follows the newest human activity.
    assert projected._attended_activity_received_at == 110
