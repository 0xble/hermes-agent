"""Single-occurrence worker inserts on the owner's receipt transaction."""
from __future__ import annotations

from hermes_state_message_insert import insert_single_message, prepare_single_message

_MESSAGE_FIELDS = frozenset({
    'role', 'content', 'tool_name', 'tool_calls', 'tool_call_id', 'token_count',
    'finish_reason', 'reasoning', 'reasoning_content', 'reasoning_details',
    'codex_reasoning_items', 'codex_message_items', 'platform_message_id', 'observed',
    'effect_disposition', '_compressed_summary', 'timestamp', 'api_content',
    'display_kind', 'display_metadata', 'message_uid',
})
_GUARD_FIELDS = frozenset({'archived', 'compression_lock_holder', 'turn_lease_holder', 'turn_lease_ttl_seconds'})


def worker_append_message(db, conn, session_id, payload):
    from hermes_state_runtime import RuntimeStoreError, _text

    if (set(payload) - (_MESSAGE_FIELDS | _GUARD_FIELDS)
            or payload.get('role') not in ('user', 'assistant', 'system', 'tool')
            or type(payload.get('archived', False)) is not bool):
        raise RuntimeStoreError('invalid_params')
    for key in ('compression_lock_holder', 'turn_lease_holder'):
        if payload.get(key) is not None:
            _text(payload[key])
    ttl = payload.get('turn_lease_ttl_seconds', 300.0)
    if type(ttl) not in (int, float) or not 0.1 <= ttl <= 3600:
        raise RuntimeStoreError('invalid_params')
    message = {key: value for key, value in payload.items() if key in _MESSAGE_FIELDS}
    params, tool_count = prepare_single_message(db, session_id, message)
    row_id = insert_single_message(db, conn, session_id, params, tool_count,
        archived=payload.get('archived', False),
        compression_lock_holder=payload.get('compression_lock_holder'),
        turn_lease_holder=payload.get('turn_lease_holder'), turn_lease_ttl_seconds=ttl)
    return {'row_id': row_id}
