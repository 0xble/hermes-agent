"""Local recovery provenance must not change a native delivery's exact-retry identity."""
from copy import deepcopy

import pytest

from gateway.session_envelope import _adapter_event, _snapshot_native, snapshot_native
from gateway.session_ingress_media import ingress_fingerprint
from hermes_state_keys import admission_fingerprint
from hermes_state_runtime import admit_session_input
from tests.fakes.runtime_owner import initialize_session_authority
from tests.gateway.test_unknown_synthetic_settles import _codec, _producer
from tests.gateway.test_unknown_automation_settles import _runner


@pytest.mark.parametrize('boundary', ['admission', 'ingress'])
def test_absorbed_provenance_is_not_delivery_identity(tmp_path, monkeypatch, boundary):
    store, runner = _runner(tmp_path, monkeypatch, [])
    _codec(runner)
    try:
        original = snapshot_native(runner, _producer('goal'))
        original['native_text_v1'].pop('absorbed_synthetic')
        untouched = deepcopy(original)

        def fingerprint(payload):
            if boundary == 'admission':
                return admission_fingerprint(canonical_target='s', payload={'input': payload})
            return ingress_fingerprint(payload)

        for trail in ([], None, [None]):
            retry = deepcopy(original)
            retry['native_text_v1']['absorbed_synthetic'] = trail
            assert fingerprint(retry) == fingerprint(original)
            assert retry['native_text_v1']['absorbed_synthetic'] == trail
        for location in ('top-level', 'metadata', 'text'):
            changed = deepcopy(original)
            if location == 'text':
                changed['text'] += ' changed'
            elif location == 'top-level':
                changed['absorbed_synthetic'] = []
            else:
                changed['native_text_v1']['event']['metadata'] = {'absorbed_synthetic': []}
            assert fingerprint(changed) != fingerprint(original)
        assert original == untouched
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('persisted_local', [False, True])
async def test_exact_legacy_native_retry_ignores_new_absorbed_provenance(tmp_path, monkeypatch, persisted_local):
    store, runner = _runner(tmp_path, monkeypatch, [])
    _codec(runner)
    try:
        authority = await initialize_session_authority(runner, profile_id='p', instance_id='retry-owner')
        event = _producer('goal')
        event.message_id = 'legacy-goal'
        entry = store.get_or_create_session(event.source)
        legacy = snapshot_native(runner, event)
        legacy['native_text_v1'].pop('absorbed_synthetic')
        if persisted_local:
            legacy['native_text_v1'].update(absorbed_synthetic=[None], attended_activity={})
        args = dict(epoch=authority.epoch, principal_id='messaging:test',
                    session_id=entry.session_id, request_id=event.message_id)
        first = admit_session_input(authority.db, payload=legacy, **args)
        # Exercise the serializer's pre-provenance retry compatibility branch. Source/auth
        # validation is not under test; reuse the source already validated above.
        def snapshot():
            return _snapshot_native(runner, _adapter_event(event), provenance={},
                                    encoded_source=legacy['native_text_v1']['source'])

        retry = snapshot()
        assert retry == legacy
        if persisted_local:
            from gateway.session_unattended_unknown import is_unattended
            assert not is_unattended({'principal_id': 'messaging:test', 'payload': retry})
        assert admit_session_input(authority.db, payload=retry, **args) == first
        event.text += ' changed'
        changed = snapshot()
        assert 'provenance' in changed['native_text_v1'], 'only exact legacy retries omit provenance'
        from hermes_state_runtime import RuntimeStoreError
        with pytest.raises(RuntimeStoreError, match='admission_conflict'):
            admit_session_input(authority.db, payload=changed, **args)
    finally:
        store.close_all_db_handles()
