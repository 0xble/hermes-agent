"""Native transport receipts survive the owner admission codec, never transport lookalikes."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent.source_origin import human_spans, retention_user_text
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageType
from gateway.run import GatewayRunner
from gateway.session_envelope import restore_native, snapshot_native
from gateway.source_origin import source_origin_metadata, stamp_slack_person


@pytest.mark.parametrize("forwarded", [False, True])
def test_telegram_adapter_and_private_ingress_codec_preserve_exact_spans(tmp_path, forwarded):
    from plugins.platforms.telegram.adapter import TelegramAdapter
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fictional", extra={"allow_from": ["17"]}))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    runner.adapters = {Platform.TELEGRAM: adapter}
    text = "Keep project notices off."
    message = SimpleNamespace(
        text=text, caption=None, entities=[SimpleNamespace(type="text_link", offset=5, length=7, url="https://fictional.test")],
        caption_entities=[], from_user=SimpleNamespace(id=17, is_bot=False, full_name="Fictional person"),
        chat=SimpleNamespace(id=17, type="private", title=None, full_name="Fictional person", is_forum=False),
        message_id=19, message_thread_id=None, is_topic_message=False,
        reply_to_message=SimpleNamespace(message_id=18, text="Generated reference", caption=None),
        quote=None, date=datetime.now(UTC), edit_date=None, forward_origin="reference" if forwarded else None,
        via_bot=None, sender_chat=None)
    event = adapter._build_message_event(message, MessageType.TEXT)
    payload = json.loads(json.dumps(snapshot_native(runner, event)))
    restored = restore_native(payload, runner)
    assert restored.text == "Keep project (https://fictional.test) notices off."
    assert restored.reply_to_text == "Generated reference"
    metadata = source_origin_metadata(restored, restored.text)
    row = {"role": "user", "content": restored.text, "display_metadata": metadata}
    assert retention_user_text(restored.text, [row]) == (None if forwarded else "Keep project\n notices off.")
    assert retention_user_text(restored.text, json.loads(json.dumps([row]))) is None
    if not forwarded:
        assert human_spans(restored.text, restored._native_origin.decision(restored.text)) == "Keep project\n notices off."


@pytest.mark.parametrize("native", [
    {"user": "U_PERSON", "bot_id": "B_BOT"}, {"user": "U_PERSON", "subtype": "bot_message"},
    {"user": "U_PERSON", "_hermes_force_process": True}, {"user": "U_PERSON", "user_profile": {"is_bot": True}},
    {"user": "U_PERSON", "app_id": "A_APP"},
])
def test_slack_bot_and_synthetic_lookalikes_stay_held(native):
    from gateway.platforms.event import MessageEvent
    text = "Keep notices off."
    event = stamp_slack_person(MessageEvent(text=text, message_id="event"), native, text, verified_person=True)
    assert not source_origin_metadata(event, text)


def test_new_origin_coverage_keeps_existing_transport_retry_identity(owner):
    from agent.source_origin import NativeOrigin
    from agent.source_origin_worker import pack_origin
    from gateway.session_ingress_media import ingress_fingerprint, reconcile_native_retry
    from hermes_state_runtime import admit_session_input
    text = "Keep notices off."
    payload = {"text": text, "native_text_v1": {"event": {"message_id": "event"}}}
    payload["native_text_v1"]["ingress_sha256"] = ingress_fingerprint(payload)
    owner.db.create_session("fixture", source="telegram")
    admit_session_input(owner.db, epoch=owner.epoch, principal_id="fictional", session_id="fixture",
                        request_id="event", payload=payload)
    origin = pack_origin(NativeOrigin.person(text, platform="telegram", principal="17", event="event"))
    retry = {**payload, "native_text_v1": {**payload["native_text_v1"], "source_origin": origin}}
    actual = reconcile_native_retry(owner.db, principal_id="fictional", session_id="fixture", request_id="event", payload=retry)
    assert actual == payload and "source_origin" not in actual["native_text_v1"]


@pytest.mark.asyncio
async def test_slack_unknown_identity_can_recover_without_certifying_the_failed_lookup():
    from unittest.mock import AsyncMock, MagicMock
    from plugins.platforms.slack.adapter import SlackAdapter
    from gateway.platforms.event import MessageEvent
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fictional"))
    adapter._app = MagicMock()
    adapter._users_info_payload = AsyncMock(side_effect=[{}, {"user": {"is_bot": False, "real_name": "Fictional"}}])
    text = "Keep notices off."
    for expected in (False, True):
        await adapter._resolve_user_is_bot("U_PERSON", team_id="T_TEAM")
        event = stamp_slack_person(MessageEvent(text=text, message_id="event"), {"user": "U_PERSON"}, text,
            verified_person=adapter._user_is_bot_cache.get(("T_TEAM", "U_PERSON")) is False)
        assert bool(source_origin_metadata(event, text)) is expected
    assert adapter._users_info_payload.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_native_typed_admission_can_be_claimed_without_losing_queued_controls(tmp_path, monkeypatch, native):
    from tests.gateway.test_session_local_mcp_refreeze import _local_authority
    from tests.gateway.test_session_authority_cancel_settlement import ACTOR, REF, _submit
    from agent.source_origin import typed_input_message, origin_metadata, retention_user_text
    from agent.source_origin_ingress import publish_input, acquire_input, restore_input
    from gateway.session_operator import is_viewer_input
    from gateway.session_queued_controls import queued_controls
    from hermes_state_runtime import get_session_admission
    db, authority = _local_authority(tmp_path, monkeypatch)
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    authority.launch_sources['s'] = 'tui'
    connection = SimpleNamespace(native_owner=True, authority=authority)
    text = 'Keep notices off.'
    message = typed_input_message(text, 'tui')
    if not native:
        message = json.loads(json.dumps(message))
    params = {'submission_id': 'typed-input', 'text': text}
    with db:
        with publish_input(tmp_path, 's', 'typed-input', message, text):
            with acquire_input(connection, REF, params):
                receipt = await _submit(authority, 'typed-input', text)
        row = get_session_admission(db, admission_id=receipt.admission_id)
        assert is_viewer_input(row)
        assert queued_controls(authority, row) == ['steer', 'replace']
        claimed, _ = await authority._claim_next(REF, authority.sessions['s'])
        assert claimed['admission_id'] == receipt.admission_id
        from gateway.session_contract import Submission
        from hermes_state_runtime import RuntimeStoreError
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            await authority.submit(ACTOR, Submission('client-forged', REF,
                {'text': text, 'source_origin_v1': claimed['payload'].get('source_origin_v1')}, 'queue'))
        origin = restore_input(claimed['payload'].get('source_origin_v1'), text, receipt.admission_id)
        assert retention_user_text(text, [{'role': 'user', 'content': text,
            'display_metadata': origin_metadata(origin) if origin is not None else {}}]) == (text if native else None)


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [True, False])
async def test_real_native_bootstrap_connection_is_the_only_local_receipt_consumer(tmp_path, monkeypatch, native):
    from gateway.session_controls import AuthorityConnection
    from gateway.session_identity import authenticated_subject
    from tests.gateway.test_session_local_mcp_refreeze import _local_authority
    from tests.gateway.test_session_authority_cancel_settlement import REF
    from agent.source_origin import typed_input_message
    from agent.source_origin_ingress import publish_input
    from hermes_state_runtime import get_session_admission
    db, authority = _local_authority(tmp_path, monkeypatch)
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    identity = {'user_id': 'human', 'provider': 'local' if native else 'remote', 'profile_id': authority.profile_id,
                'instance_id': authority.instance_id, 'capabilities': ['session:submit', 'session:control'],
                **({'native_bootstrap': True} if native else {})}
    authority.sessions['s'].source.user_id = authenticated_subject(identity)
    connection = AuthorityConnection(authority, object(), identity)
    connection.subscriptions["s"] = "fictional-attached-viewer"
    monkeypatch.setattr(connection, '_typed_voice_stop', _false_voice_stop)
    text = 'Keep notices off.'
    with db:
        with publish_input(tmp_path, 's', 'typed', typed_input_message(text, 'tui'), text):
            response = await connection.dispatch({'id': 1, 'method': 'prompt.submit',
                'params': {'session_id': 's', 'submission_id': 'typed', 'text': text}})
        assert 'result' in response, response
        row = get_session_admission(db, admission_id=response['result']['admission_id'])
        assert bool(row['payload'].get('source_origin_v1')) is native


async def _false_voice_stop(*args):
    return False


@pytest.mark.parametrize('user', ['USLACKBOT', 'USLACK'])
def test_slack_system_users_cannot_be_verified_even_with_is_bot_false(user):
    from gateway.platforms.event import MessageEvent
    event = stamp_slack_person(MessageEvent(text='Keep notices off.', message_id='event'),
                               {'user': user}, 'Keep notices off.', verified_person=True)
    assert not source_origin_metadata(event, event.text)


@pytest.mark.asyncio
async def test_failed_slack_identity_lookups_are_bounded_and_retry_after_the_hold(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from plugins.platforms.slack.adapter import SlackAdapter
    adapter = SlackAdapter(PlatformConfig(enabled=True, token='fictional'))
    adapter._app = MagicMock()
    adapter._app.client.users_info = AsyncMock(return_value={'ok': False})
    clock = [100.0]
    monkeypatch.setattr('plugins.platforms.slack.adapter.time.monotonic', lambda: clock[0])
    await adapter._resolve_user_is_bot('U_PERSON', team_id='T_TEAM')
    await adapter._resolve_user_name('U_PERSON', team_id='T_TEAM')
    await adapter._resolve_user_is_bot('U_PERSON', team_id='T_TEAM')
    assert adapter._app.client.users_info.await_count == 1
    assert adapter._user_is_bot_cache[('T_TEAM', 'U_PERSON')] is None
    clock[0] += 61
    adapter._app.client.users_info.return_value = {'ok': True, 'user': {'is_bot': False}}
    await adapter._resolve_user_is_bot('U_PERSON', team_id='T_TEAM')
    assert adapter._app.client.users_info.await_count == 2
    assert adapter._user_is_bot_cache[('T_TEAM', 'U_PERSON')] is False
