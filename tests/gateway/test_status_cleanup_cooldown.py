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
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True,message_id='one')),
        send_delegation_card=AsyncMock(return_value=SendResult(success=True,message_id='one')),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True),
        deletion_retry_after=lambda _:max(0,deadline[0]-time.monotonic()))
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
    deadline[0] = time.monotonic()+0.05
    await manager.delivered(receipt)
    assert item['retired'] and item.get('delete_attempts',0) == 0
    adapter.delete_message.assert_not_awaited()
    while pending:
        await asyncio.gather(*list(pending.values()))
    if persistent_flood:
        assert item['message_id'] == 'one' and item['delete_attempts'] == 3
        assert adapter.delete_message.await_count == 3
    else:
        assert item['message_id'] is None and item['delete_attempts'] == 1
        adapter.delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_ambiguous_send_does_not_hide_known_aggregate_anchor(tmp_path):
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
    cards=DelegationCards(SimpleNamespace(_adapter_for_source=lambda _:adapter),home=tmp_path,interval=0)
    await cards.reconcile()
    while cards.pending:
        await asyncio.gather(*list(cards.pending.values()))
    adapter.send_delegation_card.assert_not_awaited()
    assert adapter.edit_message.call_args.args[1] == 'known'
    rendered = adapter.edit_message.call_args.args[2]
    # Both cards share the known aggregate anchor; only the heading uses bold.
    assert rendered.count('\n✓ Task\n') == 2
    assert '\nA. ' not in rendered and '\nB. ' not in rendered
    assert all('**' not in line for line in rendered.splitlines()[1:])
    assert set(cards.cards['a'*32]['rows']) == {'A'}
    assert set(cards.cards['b'*32]['rows']) == {'B'}
    assert cards.cards['a'*32]['presentation_key'] == 'b'*32
    assert not any(c.get('handled') or c.get('retired') for c in cards.cards.values())


@pytest.mark.asyncio
async def test_missing_deferred_cleanup_record_is_a_noop(tmp_path):
    runner=SimpleNamespace(_adapter_for_source=lambda _:None)
    cards=DelegationCards(runner,home=tmp_path)
    detached={'source':{'chat_id':'42'}}
    adapter=SimpleNamespace(deletion_retry_after=lambda _:1)
    assert cards._defer_delete(detached,adapter) is False
