"""Regression: messaging /retry must re-admit natively under session authority.

Background: /retry re-dispatches the last user turn by building a NEW MessageEvent and
calling the gateway's message handler directly, which bypasses the adapter's
native_callback wrapper. admission matches provenance by event identity, so a freshly
built event carries no provenance and native admission refuses it as invalid_params when
gateway.multiplex_profiles is true. The fix carries the /retry event's native callback to
the re-dispatched turn (gateway/session_ingress_context.carry_callback), which is built on
the same authenticated source object.

This test reproduces the refusal on a real multiplexed authority and asserts the fix makes
the re-dispatched turn's envelope prepare with its provenance resolved. Without the fix the
re-dispatch raises AuthorityRefusal('invalid_params').
"""
import weakref
from pathlib import Path
from typing import Any, cast

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.run_runtime import initialize_gateway_runtime
from gateway.runtime_ownership import process_ownership
from gateway.session import SessionSource
from gateway.session_envelope import prepare_native
from gateway.session_ingress_context import (
    capture_provenance, native_callback, register_transport_home)

from persistence.core.errors import AuthorityRefusal
from persistence_assembly import close_stores_under


class _FakeAdapter:
    """Weakref-able stand-in for a live platform adapter (SimpleNamespace cannot weakref).

    Deliberately attribute-free: the code under test only ever identity-compares the adapter
    (``_binding`` / ``_owning_profile``), never reads transport state off it.
    """


def _reserve_homes(tmp_path, monkeypatch, names=('beta',)):
    root = tmp_path / '.hermes'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    homes = [('default', root)]
    for name in names:
        home = root / 'profiles' / name
        home.mkdir(parents=True, mode=0o700)
        (home / 'config.yaml').write_text('{}')
        homes.append((name, home))
    return root, homes


def _runner(root, homes):
    config = GatewayConfig()
    config.multiplex_profiles = True
    config._runtime_profile_homes = tuple(homes)
    return GatewayRunner(config)


@pytest.mark.asyncio
async def test_retry_redispatch_admits_natively_under_session_authority(
        tmp_path, monkeypatch):
    root, homes = _reserve_homes(tmp_path, monkeypatch)
    runner = _runner(root, homes)
    process_ownership.reserve([home for _, home in homes])
    try:
        await initialize_gateway_runtime(runner)
        register_transport_home(runner, None, root)
        adapter = _FakeAdapter()
        runner.adapters = cast(Any, {Platform.DISCORD: adapter})
        runner._is_user_authorized_for_source = lambda *a, **k: True

        source = SessionSource(
            platform=Platform.DISCORD, chat_id='probe-chat', chat_type='dm',
            user_id='probe-user', user_name='probe')
        # build_source stamps this on every inbound event; hand-built sources must carry it too,
        # otherwise the transport-owner check fail-closes to None (not_found).
        setattr(source, '_transport_adapter_ref', weakref.ref(adapter))
        entry = await runner.async_session_store.get_or_create_session(source)
        sid = entry.session_id
        runner.session_store.append_to_transcript(
            sid, {'role': 'user', 'content': 'probe ask'})
        runner.session_store.append_to_transcript(
            sid, {'role': 'assistant', 'content': 'probe answer'})

        seen = []

        async def spy_handle_message(event):
            prov = capture_provenance(runner, event)
            try:
                payload = await prepare_native(runner, event)
                seen.append(('prepared',
                             payload['native_text_v1'].get('provenance') is not None,
                             prov is not None))
            except AuthorityRefusal as exc:
                seen.append(('refused', exc.reason, prov is not None))
            return 'spy-ok'

        runner._handle_message = spy_handle_message

        retry_event = MessageEvent(
            text='/retry', message_type=MessageType.TEXT, source=source,
            message_id='probe-msg')
        with native_callback(runner, retry_event, root, None):
            await runner._handle_retry_command(retry_event)

        assert seen, 'the re-dispatched turn was never admitted'
        assert seen[0][0] == 'prepared', f'retry was refused: {seen}'
        assert seen[0][1] is True, 'provenance not resolved on re-dispatch'
        assert seen[0][2] is True, 'capture_provenance returned None inside rebind'
    finally:
        for authority in list(getattr(runner, 'session_authorities', []) or []):
            close_stores_under(authority.persistence.scope.profile_dir)
        for _, home in homes:
            process_ownership.release(home)


@pytest.mark.asyncio
async def test_authenticated_callback_rebinds_dead_transport_reference(
        tmp_path, monkeypatch):
    """A reconnect can leave an event's weak transport reference dead before callback admission.

    The callback and current registry binding are still authoritative.  The source may rebind to
    that current adapter, while a live reference to another adapter remains rejected.
    """
    root, homes = _reserve_homes(tmp_path, monkeypatch, names=())
    runner = _runner(root, homes)
    process_ownership.reserve([home for _, home in homes])
    try:
        await initialize_gateway_runtime(runner)
        register_transport_home(runner, None, root)
        adapter = _FakeAdapter()
        runner.adapters = cast(Any, {Platform.DISCORD: adapter})
        runner._is_user_authorized_for_source = lambda *a, **k: True

        source = SessionSource(
            platform=Platform.DISCORD, chat_id='probe-chat', chat_type='dm',
            user_id='probe-user', user_name='probe')
        dead_adapter = _FakeAdapter()
        setattr(source, '_transport_adapter_ref', weakref.ref(dead_adapter))
        del dead_adapter
        event = MessageEvent(text='probe', source=source, message_id='probe-msg')

        with native_callback(runner, event, root, None):
            provenance = capture_provenance(runner, event)

        assert provenance is not None
        assert runner._transport_owner(source) == (adapter, None)
    finally:
        for authority in list(getattr(runner, 'session_authorities', []) or []):
            authority.db.close()
        for _, home in homes:
            process_ownership.release(home)


@pytest.mark.asyncio
async def test_legacy_primary_native_source_restores_after_default_profile_repair(
        tmp_path, monkeypatch):
    """A pre-multiplex envelope's null primary profile must restore through the live registry."""
    root, homes = _reserve_homes(tmp_path, monkeypatch, names=())
    runner = _runner(root, homes)
    process_ownership.reserve([home for _, home in homes])
    try:
        await initialize_gateway_runtime(runner)
        register_transport_home(runner, None, root)
        adapter = _FakeAdapter()
        runner.adapters = cast(Any, {Platform.DISCORD: adapter})
        runner._is_user_authorized_for_source = lambda *a, **k: True

        source = SessionSource(
            platform=Platform.DISCORD, chat_id='probe-chat', chat_type='dm',
            user_id='probe-user', user_name='probe')
        setattr(source, '_transport_adapter_ref', weakref.ref(adapter))
        event = MessageEvent(text='probe', source=source, message_id='probe-msg')
        with native_callback(runner, event, root, None):
            provenance = capture_provenance(runner, event)

        encoded = source.to_dict()
        encoded['is_bot'] = source.is_bot
        payload = {'native_text_v1': {
            'source': encoded,
            'route': runner.session_store._generate_session_key(source),
            'provenance': provenance,
        }}
        from gateway.session_envelope import restore_native_source
        restored = restore_native_source(payload, runner)

        assert restored.profile is None
        assert runner._adapter_for_source(restored) is adapter
    finally:
        for authority in list(getattr(runner, 'session_authorities', []) or []):
            authority.db.close()
        for _, home in homes:
            process_ownership.release(home)


@pytest.mark.asyncio
async def test_authenticated_callback_rejects_missing_transport_reference(
        tmp_path, monkeypatch):
    """A callback cannot turn a source without transport proof into a native route."""
    root, homes = _reserve_homes(tmp_path, monkeypatch, names=())
    runner = _runner(root, homes)
    process_ownership.reserve([home for _, home in homes])
    try:
        await initialize_gateway_runtime(runner)
        register_transport_home(runner, None, root)
        adapter = _FakeAdapter()
        runner.adapters = cast(Any, {Platform.DISCORD: adapter})
        runner._is_user_authorized_for_source = lambda *a, **k: True

        source = SessionSource(
            platform=Platform.DISCORD, chat_id='probe-chat', chat_type='dm',
            user_id='probe-user', user_name='probe')
        event = MessageEvent(text='probe', source=source, message_id='probe-msg')

        with native_callback(runner, event, root, None):
            with pytest.raises(RuntimeStoreError, match='not_found'):
                capture_provenance(runner, event)
    finally:
        for authority in list(getattr(runner, 'session_authorities', []) or []):
            authority.db.close()
        for _, home in homes:
            process_ownership.release(home)


@pytest.mark.asyncio
async def test_authenticated_callback_rejects_live_foreign_transport_reference(
        tmp_path, monkeypatch):
    root, homes = _reserve_homes(tmp_path, monkeypatch, names=())
    runner = _runner(root, homes)
    process_ownership.reserve([home for _, home in homes])
    try:
        await initialize_gateway_runtime(runner)
        register_transport_home(runner, None, root)
        adapter = _FakeAdapter()
        foreign = _FakeAdapter()
        runner.adapters = cast(Any, {Platform.DISCORD: adapter})
        runner._is_user_authorized_for_source = lambda *a, **k: True

        source = SessionSource(
            platform=Platform.DISCORD, chat_id='probe-chat', chat_type='dm',
            user_id='probe-user', user_name='probe')
        setattr(source, '_transport_adapter_ref', weakref.ref(foreign))
        event = MessageEvent(text='probe', source=source, message_id='probe-msg')

        with native_callback(runner, event, root, None):
            with pytest.raises(RuntimeStoreError, match='not_found'):
                capture_provenance(runner, event)
    finally:
        for authority in list(getattr(runner, 'session_authorities', []) or []):
            authority.db.close()
        for _, home in homes:
            process_ownership.release(home)
