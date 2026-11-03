"""Rich Message adapter metadata survives native admission and replay without a trust grant."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from tests.fakes.runtime_owner import initialize_session_authority
from gateway.session_envelope import check_native_route, prepare_native, restore_native, snapshot_native
from gateway.session_synthetic import is_synthetic, mark_synthetic
from hermes_state_runtime import RuntimeStoreError, list_session_admissions
from tests.gateway.test_telegram_rich_message_ingress import _adapter, _message, _rich_payload, _update


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['api_kwargs', 'attribute', None])
async def test_rich_handler_metadata_is_admitted_and_rehydrated(tmp_path, monkeypatch, source):
    adapter = _adapter()
    message = _message(rich=_rich_payload())
    if source == 'attribute':
        message.rich_message = _rich_payload()
    await adapter._handle_rich_message(_update(message), None)
    event = adapter._enqueue_text_event.call_args.args[0]
    if source is None:
        event.metadata['telegram_rich_message']['source'] = None
    expected = deepcopy(event.metadata)
    assert expected['telegram_rich_message']['source'] == source

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter.gateway_runner, runner.adapters = runner, {Platform.TELEGRAM: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='rich')
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    try:
        receipt = await authority.admit_native(event)
        rows = list_session_admissions(authority.db, session_id=receipt.ref.session_id, pending_only=False)
        assert len(rows) == 1
        payload = json.loads(json.dumps(rows[0]['payload']))
        assert payload['native_text_v1']['event']['metadata'] == expected
        assert 'synthetic' not in payload['native_text_v1']
        restored = restore_native(payload, runner)
        assert restored.text == event.text
        assert restored.metadata == expected
        assert not is_synthetic(restored)
        await check_native_route(runner, payload, receipt.ref.session_id, event.source, adapter)
        assert snapshot_native(runner, restored) == payload

        # Caller, stored projection and each replay must own independent nested lists.
        event.metadata['telegram_rich_message']['block_types'].append('caller-change')
        restored.metadata['telegram_rich_message']['block_types'].append('replay-change')
        assert restore_native(payload, runner).metadata == expected

        bounded = replace(event, metadata={'telegram_rich_message': {
            'block_count': 0, 'block_types': ['x' * 64] * 256, 'source': 'x' * 64, 'truncated': True}})
        assert restore_native(await prepare_native(runner, bounded), runner).metadata == bounded.metadata

        # Private gateway producers retain their separate codec and capability on replay.
        synthetic = replace(event, internal=True, metadata=deepcopy(expected))
        mark_synthetic(synthetic)
        restored_synthetic = restore_native(snapshot_native(runner, synthetic), runner)
        assert restored_synthetic.internal is True and is_synthetic(restored_synthetic)
        assert restored_synthetic.metadata == expected
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_rich_metadata_refuses_invalid_shapes_and_unknown_keys(tmp_path):
    adapter = _adapter()
    await adapter._handle_rich_message(_update(_message(rich=_rich_payload())), None)
    event = adapter._enqueue_text_event.call_args.args[0]
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    valid = event.metadata['telegram_rich_message']
    invalid = [
        {'telegram_rich_message': value} for value in (None, True, [], 'rich')
    ]
    for field, values in {
        'block_count': [-1, True, 1.5, '1', None],
        'block_types': [None, 'paragraph', ('paragraph',), ['x'] * 257, [None], [1], ['x' * 65], ['']],
        'source': [True, 1, [], 'x' * 65, ''],
        'truncated': [None, 0, 1, 'false'],
    }.items():
        invalid.extend({'telegram_rich_message': {**valid, field: value}} for value in values)
    invalid.extend({'telegram_rich_message': {key: value for key, value in valid.items() if key != missing}}
                   for missing in valid)
    invalid.extend([
        {'telegram_rich_message': {**valid, 'internal': True}},
        {**event.metadata, 'gateway_session_id': 'forged'},
        {'whatsapp_native_type': 'conversation'},
        {'unknown': True},
        ['telegram_rich_message'],
    ])
    for metadata in invalid:
        with pytest.raises(RuntimeStoreError) as caught:
            await prepare_native(runner, replace(event, metadata=metadata))
        assert (caught.value.reason, caught.value.field) == ('invalid_params', 'metadata'), metadata

    for platform in (Platform.DISCORD, Platform.SLACK, Platform.WHATSAPP):
        foreign = replace(event, source=replace(event.source, platform=platform))
        for metadata in (event.metadata, {'unknown': True}):
            with pytest.raises(RuntimeStoreError) as caught:
                await prepare_native(runner, replace(foreign, metadata=metadata))
            assert (caught.value.reason, caught.value.field) == ('invalid_params', 'metadata')
