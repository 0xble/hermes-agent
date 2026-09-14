import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gateway.config import Platform
from gateway.delegation_cards import DelegationCards

from gateway.platforms.base import SendResult, MessageEvent
from gateway.session import SessionSource


@pytest.mark.asyncio
@pytest.mark.parametrize('persistent_flood', [False, True])
async def test_cooldown_defers_delegation_cleanup_without_consuming_attempts(persistent_flood, tmp_path):
    deadline = [0.0]
    initial_cooldown = [False]
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True,message_id='one')),
        send_delegation_card=AsyncMock(return_value=SendResult(success=True,message_id='one')),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True),
        deletion_retry_after=lambda _:0.01 if initial_cooldown[0] else max(0,deadline[0]-time.monotonic()))
    async def delete(*args):
        if persistent_flood:
            deadline[0] = time.monotonic()+0.01
            return False
        return True
    adapter.delete_message.side_effect = delete
    runner = SimpleNamespace(_adapter_for_source=lambda _:adapter, _thread_metadata_for_source=lambda _: {})
    source = SessionSource(platform=Platform.TELEGRAM,chat_id='42')
    manager = DelegationCards(runner,home=tmp_path,interval=0)
    owner = dict(profile='default',session_id='s',session_key='r',chat_id='42',thread_id='')
    data = dict(parent_task_id='a'*32,thread_ref='A',owner=owner)
    await manager.observe(source,'r','s',1,'subagent.start',None,data)
    await asyncio.gather(*list(manager.pending.values()))
    await manager.observe(source,'r','s',1,'subagent.complete',None,data)
    event = MessageEvent(source=source,text='handled',internal=True,metadata={
        'delegation_parent_task_id':'a'*32,'delegation_owner':owner,'delegation_thread_refs':['A']})
    # A terminal row is not a handled row: since #148 a card retires only on an explicit parent
    # attestation, so receipt() sees nothing to deliver until handling() has recorded one.
    await manager.handling(source,'r','s',2,actor_session_id='s',parent_task_id='a'*32,
                           refs=['A'],reason='blocker_report')
    receipt = manager.receipt(event,'r',2)
    item = manager.cards['a'*32]
    pending = manager.pending
    # Hold the cooldown until the deferred state is observed; durable receipt
    # writes may legitimately take longer than a short wall-clock window.
    initial_cooldown[0] = True
    await manager.delivered(receipt)
    assert item['retired'] and item.get('delete_attempts',0) == 0
    adapter.delete_message.assert_not_awaited()
    initial_cooldown[0] = False
    while pending:
        await asyncio.gather(*list(pending.values()))
    if persistent_flood:
        assert item['message_id'] == 'one' and item['delete_attempts'] == 3
        assert adapter.delete_message.await_count == 3
    else:
        assert item['message_id'] is None and item['delete_attempts'] == 1
        adapter.delete_message.assert_awaited_once()


@pytest.fixture
def legacy_aggregate(tmp_path):
    owner = dict(profile='default',session_id='s',session_key='r',chat_id='42',thread_id='')
    source = dict(platform='telegram',chat_id='42')
    def record(ref, mid):
        return dict(owner=owner, source=source, started_at=1, generation=1,
            rows={ref:dict(thread_ref=ref,state='completed')}, message_id=mid,
            rendered='',send_attempts=1,recoveries=0,retired=False)
    path=tmp_path/'cache/delegation/cards.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'a'*32:record('A',None),'b'*32:record('B','known')}))
    adapter=SimpleNamespace(send_delegation_card=AsyncMock(),edit_message=AsyncMock(return_value=SendResult(success=True)),
                            delete_message=AsyncMock(return_value=True))
    return path, adapter


@pytest.mark.asyncio
async def test_legacy_ambiguous_send_does_not_hide_known_aggregate_anchor(tmp_path, legacy_aggregate):
    path, adapter = legacy_aggregate
    records = json.loads(path.read_text())
    # This is a transport-fence regression, not an expiry case. Give both
    # completed rows a trustworthy, still-open display window.
    for card in records.values():
        for row in card['rows'].values():
            row.update(terminal_at=100, display_expires_at=400)
    path.write_text(json.dumps(records))
    cards=DelegationCards(SimpleNamespace(_adapter_for_source=lambda _:adapter),
                          home=tmp_path,interval=0,clock=lambda:200)
    await cards.reconcile()
    while cards.pending:
        await asyncio.gather(*list(cards.pending.values()))
    adapter.send_delegation_card.assert_not_awaited()
    assert adapter.edit_message.call_args.args[1] == 'known'
    rendered = adapter.edit_message.call_args.args[2]
    # Both completed rows share the known aggregate anchor without a heading,
    # activity sublines, reference labels, or bold markup.
    assert rendered.splitlines() == ['✓ Task', '✓ Task']
    assert 'A. ' not in rendered and 'B. ' not in rendered
    assert '**' not in rendered
    assert set(cards.cards['a'*32]['rows']) == {'A'}
    assert set(cards.cards['b'*32]['rows']) == {'B'}
    assert cards.cards['a'*32]['presentation_key'] == 'b'*32
    assert not any(c.get('handled') or c.get('retired') for c in cards.cards.values())
    adapter.delete_message.assert_not_awaited()
    assert cards.cards['b'*32]['message_id'] == 'known'
    await cards.shutdown()


@pytest.mark.asyncio
async def test_legacy_timestampless_aggregate_hides_without_losing_durable_results(
        tmp_path, monkeypatch, legacy_aggregate):
    from tools import async_delegation as ad

    path, adapter = legacy_aggregate
    records = json.loads(path.read_text())
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(ad, '_db_path', lambda: tmp_path / 'async_delegations.db')
    for key, card in records.items():
        ref = next(iter(card['rows']))
        metadata = dict(parent_task_id=key, owner=card['owner'],
                        thread_refs=[ref], attempts={ref: 0},
                        owner_json=json.dumps(card['owner'], sort_keys=True, separators=(',', ':')),
                        threads=[dict(thread_ref=ref, task_index=0)])
        ad._persist_dispatch(dict(delegation_id=key, session_key='r', parent_session_id='s',
                                  dispatched_at=1, delegation_metadata=metadata))
        ad._persist_completion(dict(delegation_id=key, status='completed', completed_at=100),
                               dict(results=[dict(task_index=0, status='completed', summary=f'Result {ref}')]))
    durable_before = {key: ad.get_durable_delegation(key) for key in records}
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    cards = DelegationCards(runner, home=tmp_path, interval=0, clock=lambda:200)
    await cards.reconcile()
    while cards.pending:
        await asyncio.gather(*list(cards.pending.values()))

    # No trustworthy row timestamps means immediate display expiry, even when
    # an ambiguous competing send fences replacement of the known transport.
    adapter.send_delegation_card.assert_not_awaited()
    adapter.edit_message.assert_not_awaited()
    adapter.delete_message.assert_awaited_once_with('42', 'known')
    assert not cards._display_projection('b'*32)['rows']
    assert cards.cards['b'*32]['message_id'] is None
    await cards.shutdown()

    restored = DelegationCards(runner, home=tmp_path, interval=0, clock=lambda:200)
    persisted = json.loads(path.read_text())
    for state in (cards.cards, persisted, restored.cards):
        assert set(state) == set(records)
        for key, original in records.items():
            card = state[key]
            assert card['owner'] == original['owner']
            assert set(card['rows']) == set(original['rows'])
            assert not card.get('handled') and not card.get('retired')
            for row in card['rows'].values():
                assert row['state'] == 'completed'
                assert 'terminal_at' not in row and 'display_expires_at' not in row
                assert not row.get('disposition')
    assert {key: ad.get_durable_delegation(key) for key in records} == durable_before
    for key, entry in durable_before.items():
        assert entry is not None
        ref = next(iter(records[key]['rows']))
        assert entry['result']['results'][0]['summary'] == f'Result {ref}'
    await restored.reconcile()
    while restored.pending:
        await asyncio.gather(*list(restored.pending.values()))
    adapter.delete_message.assert_awaited_once_with('42', 'known')
    adapter.send_delegation_card.assert_not_awaited()
    adapter.edit_message.assert_not_awaited()
    await restored.shutdown()


@pytest.mark.asyncio
async def test_missing_deferred_cleanup_record_is_a_noop(tmp_path):
    runner=SimpleNamespace(_adapter_for_source=lambda _:None)
    cards=DelegationCards(runner,home=tmp_path)
    detached={'source':{'chat_id':'42'}}
    adapter=SimpleNamespace(deletion_retry_after=lambda _:1)
    assert cards._defer_delete(detached,adapter) is False
