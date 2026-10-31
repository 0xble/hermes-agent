"""Canonical persistence for the launchd-detached cron executor.

The gateway owns admission and SQLite. The already fenced external worker owns
inference, its receipt journal and delivery. Losing an observer is not Stop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from types import SimpleNamespace

from hermes_state_runtime import RuntimeStoreError

PREFIX = 'cron-worker:'
logger = logging.getLogger(__name__)


def detached_assignment(job_id, execution_id):
    from cron.executions import get_execution
    row = get_execution(execution_id) if execution_id else None
    return row if row and row['job_id'] == job_id and row.get('owner_kind') == 'detached' else None


def detached_execution(job_id, execution_id):
    """Resolve only the scheduler's durable, birth-fenced detached assignment."""
    from cron.executions import execution_owner_live
    row = detached_assignment(job_id, execution_id)
    if (row and row['status'] in ('claimed', 'running')
            and execution_owner_live(execution_id, job_id)):
        return row
    return None


def owns_detached_execution(job_id, execution_id):
    row = detached_execution(job_id, execution_id)
    return row is not None and row['pid'] == os.getpid()


def _request(row):
    """The admitted identity is durable before the firer receives its submit answer."""
    from hermes_state_keys import CRON_PRINCIPAL_ID
    request = row.get('request_id', '').split(':', 2)
    if (row.get('principal_id') != CRON_PRINCIPAL_ID
            or len(request) != 3 or request[0] != 'cron'):
        return None
    return request


def _record(row):
    """The frozen cron identity, never a caller-selected session or job override."""
    request = _request(row)
    if request is None:
        return None
    from cron.scheduler_authority import journal_path
    path = journal_path(request[1], request[2])
    try:
        record = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    if (record.get('params', {}).get('job_id') != request[1]
            or record.get('params', {}).get('request_id') != request[2]
            or record.get('receipt') != {'session_id': row['target_session_id'],
                                          'admission_id': row['admission_id']}):
        return None
    return path, record


def _cancel_path(job_id, execution_id):
    from cron.scheduler_authority import journal_path
    return journal_path(job_id, execution_id).with_suffix('.cancel')


def cancel_detached(row):
    request = _request(row)
    if request is not None:
        from utils import atomic_json_write
        # The owner can accept Stop after claim but before the submit answer is
        # saved. A process-local event or a receipt-dependent marker loses that Stop.
        atomic_json_write(_cancel_path(request[1], request[2]),
                          {'cancelled': True}, mode=0o600, fsync_dir=True)


def detached_result(row):
    found = _record(row)
    if found is None:
        return None
    result = found[1].get('worker_result')
    if not isinstance(result, dict) or set(result) != {'result', 'job_flags'}:
        return None
    value = result['result']
    if (not isinstance(value, list) or len(value) != 4 or type(value[0]) is not bool
            or not all(isinstance(v, str) for v in value[1:3])
            or not (value[3] is None or isinstance(value[3], str))
            or not isinstance(result['job_flags'], dict)):
        raise RuntimeStoreError('invalid_cron_result')
    return result


def defer_detached(db, row):
    """Boot must not discard an admission whose actual executor is still live."""
    from hermes_state_keys import CRON_PRINCIPAL_ID
    if row.get('principal_id') != CRON_PRINCIPAL_ID:
        return False
    with db._read_ctx() as conn:
        generation = conn.execute('SELECT runtime_generation FROM sessions WHERE id=?',
                                 (row['target_session_id'],)).fetchone()
    if generation is None or generation[0] != row['generation']:
        return False
    request = row.get('request_id', '').split(':', 2)
    if len(request) != 3 or request[0] != 'cron':
        return False
    return detached_execution(request[1], request[2]) is not None or detached_result(row) is not None


def stored_result(result):
    value = result['result']
    return {'result': {'final_response': value[2], 'messages': [],
                       'completed': bool(value[0]), 'failed': not value[0],
                       'error': value[3], 'cron_result': value,
                       'cron_job_flags': result['job_flags']}, 'usage': {}}


def _log_lost_detached(row, job_id, execution_id):
    from cron.executions import _owner_identity, get_execution
    execution = get_execution(execution_id)
    identity = (_owner_identity(execution['pid'], execution['process_started_at'])
                if execution is not None else 'absent')
    details = execution or {}
    logger.warning(
        'detached_cron_worker_lost admission_id=%s job_id=%s execution_id=%s '
        'status=%s pid=%s process_id=%s process_started_at=%s owner_liveness=%s',
        row['admission_id'], job_id, execution_id, details.get('status', 'absent'),
        details.get('pid'), details.get('process_id'), details.get('process_started_at'), identity)


async def observe_detached(row):
    """Observe without taking ownership of the worker or cancelling it on teardown."""
    request = row['request_id'].split(':', 2)
    while True:
        result = await asyncio.to_thread(detached_result, row)
        if result is not None:
            return result
        if await asyncio.to_thread(detached_execution, request[1], request[2]) is None:
            # The worker publishes its receipt before terminalizing the ledger.
            # The initial read may have preceded both commits.
            result = await asyncio.to_thread(detached_result, row)
            if result is not None:
                return result
            await asyncio.to_thread(_log_lost_detached, row, request[1], request[2])
            raise RuntimeStoreError('detached_cron_worker_lost')
        await asyncio.sleep(.1)


def recover_detached(db, epoch, row):
    """Import the exact completed receipt, never requeue or execute an unknown turn."""
    result = detached_result(row)
    if result is None:
        return False
    from hermes_state_runtime import _admission, _epoch, _session, _retire_admission_workers
    from gateway.session_results import finish_result, _redacted

    def capture(conn):
        _epoch(conn, epoch)
        current = _admission(conn, row['admission_id'])
        if current['status'] == 'terminal':
            from hermes_state_terminal import RESULT_PREFIX, load_result
            saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                                 (RESULT_PREFIX + row['admission_id'],)).fetchone()
            if saved is None or load_result(saved[0])['result'].get('cron_result') != _redacted(result['result']):
                return None
            return 'settled'
        if (current['status'] not in ('unknown', 'started')
                or current['generation'] != row['generation']
                or _session(conn, row['target_session_id'])['runtime_generation'] != row['generation']):
            return None
        # Retire the captured executor under its original epoch before moving
        # the admission. Settlement can only retire workers owned by the new epoch.
        _retire_admission_workers(conn, current, current['owner_epoch'])
        # This is adoption of a captured completion, not a new execution. The
        # same journal survives a crash between this stamp and settlement.
        conn.execute("UPDATE session_admissions SET status='started',owner_epoch=? WHERE admission_id=?",
                     (epoch, row['admission_id']))
        return 'captured'

    captured = db._execute_write(capture)
    if captured is None:
        return False
    if captured == 'captured':
        finish_result(db, epoch=epoch, row=row, response=result['result'][2],
                      outcome='completed', result=stored_result(result))
    from utils import atomic_json_write
    found = _record(row)
    if found is not None:
        path, record = found
        record['canonical_settled'] = True
        atomic_json_write(path, record, mode=0o600, fsync_dir=True)
    return True


async def register_detached(connection, ref, params, claim):
    """Only the durable detached PID can attach to its already-started cron admission."""
    from hermes_state_runtime import register_worker_execution
    from hermes_state_keys import CRON_PRINCIPAL_ID, cron_request_id
    from cron.executions import get_execution
    authority = connection.authority
    eid = params['execution_id'].removeprefix(PREFIX)
    ledger = await asyncio.to_thread(get_execution, eid)
    live = await asyncio.to_thread(detached_execution, ledger['job_id'], eid) if ledger else None
    if (not params['execution_id'].startswith(PREFIX) or live is None
            or live['pid'] != params['pid']):
        raise RuntimeStoreError('permission_denied')
    request = cron_request_id(live['job_id'], eid)
    with authority.db._read_ctx() as conn:
        row = conn.execute("SELECT * FROM session_admissions WHERE target_session_id=? "
                           "AND principal_id=? AND request_id=?", (ref.session_id, CRON_PRINCIPAL_ID, request)).fetchone()
        if (row is None or row['status'] != 'started' or row['owner_epoch'] != authority.epoch
                or row['generation'] != params['generation']):
            raise RuntimeStoreError('producer_not_started')
    return await asyncio.to_thread(register_worker_execution, authority.db, epoch=authority.epoch,
        execution_id=params['execution_id'], session_id=ref.session_id, generation=params['generation'],
        kind='cron', adoption_secret=claim)


def run_detached(job, receipt, state, record, save, *, execution_id, extra_prompt, cancel_event):
    """Run in the existing external worker, with the canonical owner's receipt facade."""
    from agent.runtime_session_store import RuntimeSessionStore, WorkerRPC, WorkerPersistenceError
    from gateway.session_cron import bind_execution, _execution
    from hermes_cli.process_identity import process_birth
    from hermes_constants import get_hermes_home
    from hermes_state_errors import GatewayOwnerRestartError
    from websockets.exceptions import ConnectionClosed
    import psutil

    if not owns_detached_execution(job['id'], execution_id):
        raise RuntimeStoreError('permission_denied')
    scope = dict(profile_id=state['profile_id'], session_id=receipt['session_id'],
                 execution_id=PREFIX + execution_id, generation=state['generation'],
                 pid=os.getpid(), birth=process_birth(psutil.Process()), secret=secrets.token_urlsafe(32))
    rpc = WorkerRPC(get_hermes_home())
    rpc('worker.register', **scope, kind='cron')
    adopted = rpc('worker.adopt', **scope)
    scope['epoch'] = adopted['owner_epoch']
    store = None
    stopping = threading.Event()
    cancel_event = cancel_event or threading.Event()

    def reconnect(method, **params):
        nonlocal scope
        while True:
            try:
                return rpc(method, **params)
            except (WorkerPersistenceError, GatewayOwnerRestartError, ConnectionClosed, OSError, TimeoutError) as exc:
                # A returned authorization/assignment refusal is terminal. Only
                # owner absence and the released epoch are lifecycle events.
                # Detached executions outlive each bounded pre-send retry window.
                # WorkerRPC never blindly resends an ambiguous send. After
                # authenticated adoption, this wrapper replays the identical
                # journal entry, deduplicated by the owner's stored receipt.
                if isinstance(exc, WorkerPersistenceError) and str(exc) not in (
                        'owner_unavailable', 'gateway_unavailable', 'storage_unavailable', 'stale_epoch'):
                    raise
                try:
                    current = store.scope if store is not None else scope
                    next_owner = rpc('worker.adopt', **{k: v for k, v in current.items() if k != 'epoch'})
                except (WorkerPersistenceError, GatewayOwnerRestartError, ConnectionClosed, OSError, TimeoutError) as adoption:
                    if isinstance(adoption, WorkerPersistenceError) and str(adoption) not in (
                            'owner_unavailable', 'gateway_unavailable', 'storage_unavailable', 'stale_epoch'):
                        raise
                    time.sleep(.1)
                    continue
                scope['epoch'] = next_owner['owner_epoch']
                params['epoch'] = scope['epoch']
                if store is not None:
                    store.adopt(scope['epoch'])

    outbox = get_hermes_home() / 'cron' / 'worker-receipts' / execution_id
    store = RuntimeSessionStore(reconnect, scope, outbox, adopted=adopted)
    owner = SimpleNamespace(db=store)
    token = bind_execution(owner, receipt['session_id'], job['id'], execution_id)

    def watch_cancel():
        while not stopping.wait(.1):
            if _cancel_path(job['id'], execution_id).exists():
                cancel_event.set()
                return

    watcher = threading.Thread(target=watch_cancel, daemon=True)
    watcher.start()
    try:
        from cron.scheduler import run_job
        value = run_job(job, execution_id=execution_id, extra_prompt=extra_prompt, cancel_event=cancel_event)
        from gateway.session_cron import RUN_JOB_FLAGS
        record['worker_result'] = {'result': list(value),
                                  'job_flags': {k: job[k] for k in RUN_JOB_FLAGS if k in job}}
        save()
        return value
    finally:
        _execution.reset(token)
        stopping.set()
        watcher.join(timeout=1)
        rpc.close()
