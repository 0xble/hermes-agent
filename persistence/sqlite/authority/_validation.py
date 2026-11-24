"""Validate legacy runtime mutation payloads before the authority applies them."""
from __future__ import annotations

import time

METADATA_FIELDS = {'title': str, 'archived': bool, 'hidden': bool, 'pinned': bool, 'unread': bool}


def _invalid():
    from hermes_state_runtime import RuntimeStoreError
    raise RuntimeStoreError('invalid_params')


def validate_action(operation, payload):
    if not isinstance(payload, dict) or not isinstance(operation, str):
        _invalid()
    if (operation == 'model' and 'model' in payload and not set(payload) - {'model', 'provider', 'confirm', 'reasoning'}
            and all(isinstance(v, str) and v.strip() for v in payload.values())):
        from hermes_constants import parse_reasoning_effort
        if 'reasoning' not in payload or parse_reasoning_effort(payload['reasoning']) is not None:
            return
    if operation == 'branch' and _valid_branch_payload(payload): return
    if operation == 'reasoning' and set(payload) == {'effort'} and payload['effort'] is None: return
    if operation == 'reasoning' and set(payload) == {'effort'} and isinstance(payload['effort'], str):
        from hermes_constants import parse_reasoning_effort
        if parse_reasoning_effort(payload['effort']) is not None: return
    if operation == 'workspace' and set(payload) == {'cwd'} and isinstance(payload['cwd'], str) and payload['cwd'].strip(): return
    if operation == 'editor_mcp' and set(payload) == {'mcp_servers'} and _valid_editor_fingerprint(payload): return
    if (operation == 'edit_policy' and set(payload) == {'policy'} and isinstance(payload['policy'], str)
            and payload['policy'] in {'ask', 'workspace_session', 'session'}): return
    if operation == 'compress' and _valid_compress_payload(payload): return
    if operation in {'delete', 'reset'} and not payload: return
    if operation == 'import' and set(payload) == {'sessions'} and isinstance(payload['sessions'], list): return
    if (operation == 'rewind' and 'target_message_id' in payload
            and not set(payload) - {'target_message_id', 'preserve_compaction_handoff'}
            and type(payload['target_message_id']) is int and payload['target_message_id'] > 0
            and type(payload.get('preserve_compaction_handoff', False)) is bool): return
    required = {'rename': {'title'}, 'archive': {'archived'}}
    valid = bool(payload) and not set(payload) - METADATA_FIELDS.keys() if operation == 'sidebar' else (
        operation in required and set(payload) == required[operation])
    if not valid or any(type(value) is not METADATA_FIELDS[key] for key, value in payload.items()): _invalid()


def _valid_branch_payload(payload):
    if set(payload) - {'title', 'through_message_id', 'mcp_servers'}: return False
    if 'mcp_servers' in payload and not _valid_editor_fingerprint(payload): return False
    if 'title' in payload and not isinstance(payload['title'], str): return False
    return 'through_message_id' not in payload or (type(payload['through_message_id']) is int and payload['through_message_id'] > 0)


def _valid_editor_fingerprint(payload):
    servers = payload['mcp_servers']
    return isinstance(servers, dict) and set(servers) == {'fingerprint'} and isinstance(servers['fingerprint'], str)


_COMPRESS_FIELDS = {'focus': str, 'preview': bool, 'partial': bool, 'keep_last': int}


def _valid_compress_payload(payload):
    if set(payload) - _COMPRESS_FIELDS.keys(): return False
    if any(type(value) is not _COMPRESS_FIELDS[key] for key, value in payload.items()): return False
    return 'keep_last' not in payload or (payload.get('partial') is True and payload['keep_last'] >= 1)
