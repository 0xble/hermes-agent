"""Concrete runtime ownership, separate from transcript and result content.

The generic state.db parser still serves routing/import writers. Runtime receipt
families instead declare identity fields and resolve durable ownership on the
writer connection before their handler may mutate anything.
"""
import json


# Adding a receipt verb requires declaring its ownership here as well as its handler.
_ASSIGNED_ONLY = frozenset({
    'session.title', 'session.title_source', 'session.next_title', 'session.title_collision',
    'session.activity', 'session.activity_clear', 'session.billing_route', 'session.api_content',
    'session.message_api_content', 'session.user_content', 'session.end', 'session.lifecycle',
    'session.context', 'session.prompt', 'session.sidecars', 'session.tools', 'session.tail_role',
    'session.row_role', 'transcript.append', 'transcript.append_one', 'execution.finish',
    'usage.main', 'usage.auxiliary', 'turn.acquire', 'turn.renew', 'turn.release', 'turn.expires',
    'compression.append', 'compression.reopen', 'compression.watermark', 'compression.archive',
    'compression.lock.acquire', 'compression.lock.renew', 'compression.lock.release', 'compression.lock.holder',
    'compression.cooldown.record', 'compression.cooldown.restore', 'compression.cooldown.clear',
    'compression.fallback', 'compression.ineffective', 'compression.recovery', 'child.reserve',
    'delegation.child', 'delegation.read', 'delegation.claim', 'delegation.unscheduled',
    'delegation.ack', 'delegation.drop', 'delegation.defer', 'delegation.release',
})
_TARGET_FIELDS = {
    'session.create': ('parent_session_id', 'session_key'),
    'compression.publish': ('child_session_id',),
    'compression.context': ('target',), 'compression.lineage': ('target',),
    'compression.history': ('target',), 'compression.cleanup': ('target',),
    'turn.cleanup': ('target',), 'compression.boundary': ('session_key',),
    'delegation.dispatch': ('parent_session_id', 'session_key', 'origin_ui_session_id', 'origin_session_id'),
    'delegation.complete': (),
}
_ROUTE_FIELDS = ('parent_session_id', 'session_key', 'origin_ui_session_id', 'origin_session_id')
_PROVENANCE_FIELDS = ('_branched_from', '_delegate_from', '_reset_from', '_compressed_from')


def _refuse(reason='privacy_denied'):
    from hermes_state_runtime import RuntimeStoreError
    raise RuntimeStoreError(reason) from None


def _identity(value):
    if not isinstance(value, str) or not value:
        _refuse('invalid_params')
    return value


def _optional_owners(record, fields):
    if not isinstance(record, dict):
        _refuse('invalid_params')
    return tuple(_identity(record[field]) for field in fields if record.get(field) not in (None, ''))


def worker_owners(session_id, operation, payload):
    """Project only fields whose closed handler treats as session ownership."""
    if (not isinstance(payload, dict) or not isinstance(operation, str)
            or operation not in _ASSIGNED_ONLY and operation not in _TARGET_FIELDS):
        _refuse('invalid_params')
    owners = (_identity(session_id), *_optional_owners(payload, _TARGET_FIELDS.get(operation, ())))
    if operation in {'compression.context', 'compression.lineage', 'compression.history',
                     'compression.cleanup', 'turn.cleanup'}:
        _identity(payload.get('target'))
    if operation == 'compression.publish':
        _identity(payload.get('child_session_id'))
    if operation in {'session.create', 'compression.publish'} and payload.get('model_config') is not None:
        owners += _optional_owners(payload['model_config'], _PROVENANCE_FIELDS)
    if operation == 'delegation.complete':
        owners += _optional_owners(payload.get('event'), _ROUTE_FIELDS)
    return owners


def require_public(owners):
    """Lookup failure and revoked producer ancestry are refusals, never empty receipts."""
    try:
        from agent.incognito import is_incognito
        blocked = is_incognito() or any(is_incognito(_identity(sid)) for sid in owners)
    except Exception:  # health: allow BLE001 -- deny every lookup failure; raw text/tracebacks may expose private data.
        _refuse()
    if blocked:
        _refuse()


def session_owners(conn, owners):
    from hermes_state_runtime_workers import runtime_lineage
    found, expanded = set(owners), set()
    for sid in owners:
        if sid in expanded:
            continue
        lineage = runtime_lineage(conn, sid)
        expanded.update(lineage)
        found.update(lineage)
        for target in lineage:
            row = conn.execute('SELECT session_key FROM sessions WHERE id=?', (target,)).fetchone()
            if row and row[0]:
                found.add(_identity(row[0]))
    return tuple(found)


def resolve_worker_owners(conn, owners, operation, payload, execution_id):
    if operation == 'compression.lineage':
        from hermes_state_worker_compression import CompressionSnapshot
        owners += tuple(CompressionSnapshot(None, conn)._session_lineage_root_to_tip(owners[0]))
    # A reserved child's authority belongs to its durable parent execution, whereas
    # an ordinary /branch is a separate conversation, not its parent's private work.
    parent = conn.execute("SELECT s.parent_session_id FROM worker_executions w JOIN sessions s ON s.id=w.session_id "
                          "WHERE w.execution_id=? AND w.kind='child'", (execution_id,)).fetchone()
    if parent and parent[0]:
        owners += (_identity(parent[0]),)
    if operation.startswith('delegation.') and operation != 'delegation.dispatch':
        record = payload['event'] if operation == 'delegation.complete' else payload
        delegation_id = _identity(record.get('delegation_id'))
        row = conn.execute('SELECT parent_session_id,origin_session,origin_ui_session_id,origin_session_id '
                           'FROM async_delegations WHERE delegation_id=?', (delegation_id,)).fetchone()
        if row:
            owners += tuple(value for value in row if value)
    if operation == 'session.title' and payload.get('title'):
        # Title conflict repair may clear a stale automatic title on another row.
        row = _title_owner(conn, owners[0], payload['title'])
        if row:
            owners += (row[0],)
    if operation == 'execution.finish':
        owners += _child_worker_owners(conn, execution_id)
    return session_owners(conn, owners)


def _title_owner(conn, session_id, title):
    from hermes_state_titles import SessionTitlesMixin
    try:
        title = SessionTitlesMixin.sanitize_title(title)
    except ValueError:
        # An invalid title cannot clear another row. Preserve the handler's
        # existing validation/error receipt instead of changing its public API.
        return None
    return conn.execute('SELECT id FROM sessions WHERE title=? AND id!=?',
                        (title, session_id)).fetchone()


def _child_worker_owners(conn, execution_id):
    from hermes_state_runtime_workers import descendant_range
    return tuple(row[0] for row in conn.execute('SELECT session_id FROM worker_executions '
        "WHERE execution_id>=? AND execution_id<? AND status!='terminal'", descendant_range(execution_id)))


def execute_owned_write(db, write, owners, *, resolve=None, **kwargs):
    """Reuse SessionDB's lock/admission/commit path, with an explicit non-ACK on skip.

    Preflight keeps known private owners out of SQL entirely. Resolve again on the
    writer's connection: a concurrent lineage edit cannot bypass the final check.
    No privacy admission is held while waiting for the DB lock.
    """
    owners = tuple(owners)
    require_public(owners)
    if resolve is not None:
        with db._read_ctx() as conn:
            owners = tuple(dict.fromkeys((*owners, *_require_resolved_public(resolve, conn))))

    def guarded(conn):
        if resolve is not None:
            _require_resolved_public(resolve, conn)
        else:
            require_public(owners)
        return write(conn)

    guarded._state_operation = getattr(write, '_state_operation', 'runtime')
    skipped = object()
    result = db._execute_write(guarded, session_ids=owners, skip_result=skipped, **kwargs)
    if result is skipped:
        _refuse()
    return result


def _require_resolved_public(resolve, conn):
    from hermes_state_runtime import RuntimeStoreError
    try:
        owners = resolve(conn)
    except RuntimeStoreError:
        raise
    except Exception:  # health: allow BLE001 -- deny every lookup failure; raw text/tracebacks may expose private data.
        # A failed ownership lookup cannot silently omit the inaccessible owner.
        _refuse()
    require_public(owners)
    return owners


def admission_owners(conn, admission_id, transfers=()):
    from hermes_state_runtime import _admission
    row = _admission(conn, admission_id)
    lineage = json.loads(row['lineage_json'])
    if not isinstance(lineage, list):
        _refuse()
    owners = (_identity(row['target_session_id']), *(_identity(sid) for sid in lineage))
    owners += input_owners(row['target_session_id'], json.loads(row['payload_json']))
    # Settlement retires the linked execution and its live descendants too.
    for worker in conn.execute('SELECT execution_id FROM worker_executions '
            'WHERE generation=? AND owner_epoch=? AND session_id IN (SELECT value FROM json_each(?))',
            (row['generation'], row['owner_epoch'], json.dumps(lineage))):
        owners += _child_worker_owners(conn, worker[0])
    for transfer in transfers or ():
        owners += admission_owners(conn, transfer)
    return session_owners(conn, owners)


def input_owners(session_id, payload):
    """Versioned native envelopes own routes/targets. Arbitrary input metadata does not."""
    owners = (_identity(session_id),)
    if not isinstance(payload, dict):
        _refuse('invalid_params')
    if 'local_automation_v1' in payload:
        record = payload['local_automation_v1']
        if not isinstance(record, dict):
            _refuse('invalid_params')
        owners += _optional_owners(record, ('owner', 'target', 'route', 'heartbeat'))
    if 'native_text_v1' in payload:
        envelope = payload['native_text_v1']
        if not isinstance(envelope, dict):
            _refuse('invalid_params')
        owners += _optional_owners(envelope, ('route',))
        if 'automation' in envelope:
            owners += _optional_owners(envelope['automation'], ('owner', 'heartbeat'))
    return owners


def mutation_owners(db, session_id, operation, payload, prepared=None):
    owners = (_identity(session_id),)
    require_public(owners)
    if operation == 'branch' and prepared is not None:
        owners += (_identity(prepared.get('child')),)
    if operation == 'import':
        # Projection is not import validation. Even an empty/invalid import must
        # reach exact retry conflict detection before the handler validates it.
        # Valid members still carry their owners when another member is invalid.
        normalized, _errors = db._validate_import_payload(payload['sessions'])
        for item in normalized:
            record = item['session']
            owners += (_identity(record['id']), *_optional_owners(record, ('parent_session_id', 'session_key')))
            if record.get('model_config'):
                owners += _optional_owners(json.loads(record['model_config']), _PROVENANCE_FIELDS)
    return owners


def resolve_mutation_owners(conn, owners, operation, payload):
    if operation == 'delete':
        from hermes_state_mutation_guards import delete_targets
        owners += tuple(delete_targets(conn, owners[0]))
    if operation in {'rename', 'sidebar'} and payload.get('title'):
        row = _title_owner(conn, owners[0], payload['title'])
        if row:
            owners += (row[0],)
    return session_owners(conn, owners)
