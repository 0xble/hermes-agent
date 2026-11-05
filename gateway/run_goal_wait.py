"""The exact parked-goal wait is acknowledged only by its durable admission transaction."""
import copy
import json
import logging
from time import monotonic, time

from hermes_state_runtime import RuntimeStoreError

logger = logging.getLogger(__name__)


def goal_wait_identity(authority, session_id, state):
    """A goal instance and wait generation survive retries; no per-attempt UUID."""
    owner = authority.logical_owner(session_id) if authority is not None else session_id
    return json.dumps(['goal-wait', owner, state.created_at, state.waiting_since],
                      separators=(',', ':'))


def goal_wait_guard(authority, event):
    """Fenced acknowledgement in the same writer transaction as a NEW admission.

    Exact durable replays do not run authorization twice. The callback has no external
    effects and may run again on SQLite retry; a failed insert rolls it back too.
    """
    metadata = copy.deepcopy(event.metadata)
    runner = authority.runner
    key = metadata.get('gateway_session_key')
    waiting_since = metadata.get('goal_waiting_since', 0)

    def acknowledge(conn):
        from hermes_cli.goals import GoalState, _meta_key
        from hermes_cli.session_controls import _definition_fingerprint, _wake_session_matches
        stamped = metadata.get('goal_continuation_session_id')
        entry = runner.session_store.lookup_by_session_key(key)
        if (entry is None or entry.suspended or not stamped
                or not _wake_session_matches(stamped, entry.session_id, cursor=conn)):
            raise RuntimeStoreError('admission_conflict', field='goal_session')
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (_meta_key(entry.session_id),)).fetchone()
        raw = row[0] if row else None
        state = GoalState.from_json(raw) if raw else None
        if (state is None or state.status != 'active'
                or state.created_at != metadata.get('goal_continuation_instance')
                or state.waiting_since != waiting_since or waiting_since <= 0
                or _definition_fingerprint('goal', raw) != metadata.get('goal_continuation_fingerprint')):
            raise RuntimeStoreError('admission_conflict', field='goal_wait')
        # All reads and writes use this connection. Never a nested GoalManager save.
        state.last_automatic_continuation_at = max(state.last_automatic_continuation_at, time())
        state.clear_wait(preserve_notice_key=True)
        from uuid import uuid4
        state.mutation_id = uuid4().hex
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (state.to_json(), _meta_key(entry.session_id)))
    return acknowledge


def _retry_due(runner, sid, identity):
    retries = getattr(runner, '_goal_wait_retries', {})
    prior = retries.get(sid)
    return prior is None or prior[0] != identity or monotonic() >= prior[2]


def _retry_later(runner, sid, identity):
    retries = getattr(runner, '_goal_wait_retries', None)
    if retries is None:
        retries = runner._goal_wait_retries = {}
    prior = retries.get(sid)
    attempts = min(4, prior[1] + 1) if prior and prior[0] == identity else 0
    retries[sid] = (identity, attempts, monotonic() + min(60, 5 * 2 ** attempts))


async def dispatch_goal_wait(runner, adapter, source, key, sid, manager, prompt, resume_marker):
    from gateway.session_authorities import active_authority
    authority = active_authority(runner)
    managed = authority is not None or hasattr(runner, 'session_authority') or hasattr(runner, 'session_authorities')
    state = manager.state
    identity = await runner._run_in_executor_with_context(goal_wait_identity, authority, sid, state)
    if not _retry_due(runner, sid, identity):
        return
    event = runner._synthetic_prompt_event(source, prompt, reply_expected=False, goal_continuation=True,
                                           goal_session_id=sid, goal_state=state)
    event.metadata.update(gateway_session_key=key, gateway_session_id=sid, gateway_session_strict=True,
                          goal_continuation_created_at=state.waiting_since,
                          goal_waiting_since=state.waiting_since)
    reservation = runner._reserve_background_turn(key, event)
    if reservation is None:
        return
    try:
        cleared = await runner._clear_stale_resume_pending(key, resume_marker, sid, 'goal wakeup')
    except BaseException:
        runner._release_background_turn(reservation)
        raise
    if not cleared:
        runner._release_background_turn(reservation)
        return
    # The admission owns the reservation even if its observer is cancelled while
    # its shielded writer continues. A successful reservation stays until claim.
    import asyncio
    operation = _dispatch_reserved(runner, authority, managed, adapter, source, key, sid,
                                   manager, prompt, event, identity, reservation)
    if authority is not None:
        from gateway.session_runtime_workers import track_mutation
        task = track_mutation(authority, operation)
    else:
        task = asyncio.create_task(operation)
    await asyncio.shield(task)


async def _dispatch_reserved(runner, authority, managed, adapter, source, key, sid,
                             manager, prompt, event, identity, reservation):
    from gateway.session_route_wake import admit_route_wake_receipt
    admitted = False
    try:
        if managed:
            if authority is None:
                _retry_later(runner, sid, identity)
                return
            receipt = await admit_route_wake_receipt(
                runner, adapter, source, prompt, 'goal-wait', event=event, identity=identity,
                _authorize_write=goal_wait_guard(authority, event))
            if receipt is None:
                _retry_later(runner, sid, identity)
                return
            # Durable transaction already stamped dispatch and cleared only this wait.
        else:
            await _classic_dispatch(runner, adapter, event, manager, sid, manager.state.waiting_since)
        admitted = True
        getattr(runner, '_goal_wait_retries', {}).pop(sid, None)
        logger.info('goal wakeup: continuation for session %s admitted', sid)
    except Exception as exc:
        # A producer can lose its receipt after commit. Keep capacity for that row,
        # not for a failed attempt, while the stable identity prevents another row.
        if authority is not None:
            import asyncio
            from hermes_state_runtime import find_session_admission
            row = await asyncio.to_thread(lambda: find_session_admission(authority.db,
                principal_id='automation:' + key, session_id=authority.logical_owner(sid), request_id=identity))
            admitted = row is not None
        _retry_later(runner, sid, identity)
        logger.info('goal wakeup: continuation for session %s refused (%s); retrying admission',
                    sid, exc, exc_info=True)
    finally:
        if not admitted:
            runner._release_background_turn(reservation)


async def _classic_dispatch(runner, adapter, event, manager, sid, since):
    """Unmanaged embeddings retain their adapter-only contract, never a managed fallback."""
    from gateway.wake import admit_internal_event
    from hermes_cli.goals import GoalManager
    await admit_internal_event(adapter, event)
    accepted_sid = str(event.metadata.get('goal_continuation_session_id') or sid)
    anchor = manager if accepted_sid == sid else GoalManager(session_id=accepted_sid)
    await runner._run_in_executor_with_context(lambda: anchor.mark_automatic_continuation_dispatched(
        expected_instance=event.metadata.get('goal_continuation_instance'),
        expected_fingerprint=event.metadata.get('goal_continuation_fingerprint')))
    await runner._run_in_executor_with_context(manager.clear_lifted_wait, since)
