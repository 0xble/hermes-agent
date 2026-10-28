"""Legacy delegated automation cannot manufacture native trust or trip an outage canary."""
from copy import deepcopy
from queue import Queue

import pytest

from gateway import admission_canary
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_automation_upgrade_admission import upgraded_chat as upgraded_chat


@pytest.mark.asyncio
@pytest.mark.parametrize('relay_adapter', [False, True])
async def test_restarted_legacy_relay_events_drop_without_canary_retry_spike(
        upgraded_chat, monkeypatch, relay_adapter, caplog):
    runner, _, authority, entry, queued, turns, _ = upgraded_chat
    restored = runner.session_store.lookup_by_session_key(entry.session_key)
    restored.origin.delivered_via_upstream_relay = True
    if relay_adapter:
        from gateway.config import Platform, PlatformConfig
        from gateway.relay.adapter import RelayAdapter
        from gateway.relay.descriptor import CapabilityDescriptor
        descriptor = CapabilityDescriptor(
            contract_version=1, platform=entry.origin.platform.value, label='fixture',
            max_message_length=4096, supports_draft_streaming=False, supports_edit=True,
            supports_threads=True, markdown_dialect='plain', len_unit='chars')
        runner.adapters = {Platform.RELAY: RelayAdapter(PlatformConfig(enabled=True), descriptor)}
    from gateway import session_ingress_context
    monkeypatch.setattr(session_ingress_context, '_binding',
                        lambda *args: pytest.fail('legacy relay minted delegated provenance'))
    pending = Queue()
    event_count = 12  # More than the default canary threshold, even before retries.
    for index in range(event_count):
        event = deepcopy(queued)
        event['event_id'] = f'legacy-relay-{index}'
        pending.put(event)
    for _ in range(event_count * 8):
        assert not pending.empty(), 'a queued event disappeared before its permanent retry cap'
        event = pending.get_nowait()
        assert await runner._inject_watch_notification('Verifier READY', event) is False
        assert event['_refusal_permanent'] is True
        runner._requeue_refused_watch_event(pending, event)
    assert pending.empty(), 'permanent legacy refusals must not requeue forever'
    assert sum('dropping the queued event' in record.message for record in caplog.records) == event_count
    assert admission_canary.snapshot()['refusals'] == []
    assert turns == []
    assert list_session_admissions(authority.db, session_id=entry.session_id, pending_only=False) == []


@pytest.mark.asyncio
async def test_native_and_unknown_delegation_refusals_still_count(monkeypatch):
    from collections import deque
    from hermes_state_runtime import RuntimeStoreError
    monkeypatch.setattr(admission_canary, '_events', deque(maxlen=4096))
    for source, reason, field in [
        ('native', 'not_found', 'automation_legacy_delegation'),
        ('automation', 'permission_denied', 'automation_legacy_delegation'),
        ('automation', 'not_found', 'automation_route_unavailable'),
    ]:
        with pytest.raises(RuntimeStoreError), admission_canary.refusal_evidence(source):
            raise RuntimeStoreError(reason, field=field)
    assert len(admission_canary.snapshot()['refusals']) == 3
