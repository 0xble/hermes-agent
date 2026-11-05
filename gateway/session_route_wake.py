"""Authority-backed producer receipts; an adapter task is not durable admission."""
import asyncio
import json
import uuid

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from hermes_state_runtime import RuntimeStoreError


async def admit_route_wake_receipt(runner, adapter, source, text, producer, metadata=None, *,
                                   event=None, identity=None, _authorize_write=None,
                                   _authorize_write_factory=None):
    """Return a committed receipt, or None without borrowing a classic adapter path.

    Strict producers keep their original session pin. On a transient route miss, refresh
    once; automation's owner check and the producer's transaction guard retain their fences.
    """
    from gateway.session_authorities import active_authority
    from gateway.session_local_recovery import restore_completion_route
    authority = active_authority(runner)
    key = runner._session_key_for_source(source)
    restore_completion_route(runner, key)  # a closed local session boot left unrestored has no store entry yet
    if authority is None:
        return None
    identity = identity or json.dumps([producer, key, uuid.uuid4().hex], separators=(',', ':'))
    for attempt in range(2):
        # LiveSession publication belongs to this loop, not the capture worker.
        captured_source = source
        if source.platform == Platform.LOCAL:
            live = authority.sessions.get(source.chat_id)
            if live is None or live.route != key:
                return None
            captured_source = live.source
        wake = await asyncio.to_thread(_capture, authority, adapter, captured_source, text, key, identity,
                                       metadata, event)
        if wake is None:
            return None
        try:
            # Refresh the immutable guard snapshot for each route retry, outside
            # the writer. The transaction will reject any later durable change.
            guard = (await asyncio.to_thread(_authorize_write_factory)
                     if _authorize_write_factory is not None else _authorize_write)
            kwargs = {'authorize_write': guard} if guard is not None else {}
            return await authority.admit_automation(adapter, wake, identity, **kwargs)
        except RuntimeStoreError as exc:
            if exc.reason != 'not_found' or attempt:
                raise
    return None


def _capture(authority, adapter, source, text, key, identity, metadata, event):
    from gateway.session_automation import committed_route_envelope, route_rebound
    from gateway.session_envelope import restore_native
    from gateway.session_synthetic import project_synthetic, restore_synthetic
    runner = authority.runner
    entry = runner.session_store.lookup_by_session_key(key)
    if entry is None:
        return None
    if source.platform != Platform.LOCAL:
        envelope = committed_route_envelope(authority, entry)
        if envelope is None:
            return None
        source = (runner._restored_source(entry) if route_rebound(entry, envelope) else
                  restore_native({'text': text, 'native_text_v1': {
                      k: v for k, v in envelope.items() if k != 'media'}}, runner).source)
    wake = MessageEvent(text=text, message_type=MessageType.TEXT, source=source, internal=True,
                        message_id=identity, metadata=dict(metadata or {}))
    if event is not None and (synthetic := project_synthetic(event)) is not None:
        restore_synthetic(wake, synthetic)
        wake.reply_expected = event.reply_expected
        wake.inherit_channel_pin = event.inherit_channel_pin
    strict = wake.metadata.get('gateway_session_strict')
    pinned = wake.metadata.get('gateway_session_id')
    if strict and pinned:
        from hermes_cli.session_controls import _wake_session_matches
        with authority.db._read_ctx() as conn:
            if not _wake_session_matches(pinned, entry.session_id, cursor=conn):
                raise RuntimeStoreError('admission_conflict', field='goal_session')
    if wake.metadata.get('goal_waiting_since'):
        # Canonical root pins keep a retry's payload stable even when compression
        # migrated the wait between the two producer observations.
        owner = authority.logical_owner(pinned)
        wake.metadata.update(gateway_session_id=owner, goal_continuation_session_id=owner,
                             goal_continuation_created_at=wake.metadata['goal_waiting_since'])
    wake.metadata['gateway_session_key'] = key
    if not strict:
        wake.metadata['gateway_session_id'] = entry.session_id
    return wake
