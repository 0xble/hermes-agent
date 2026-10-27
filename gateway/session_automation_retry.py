"""Retry pre-execution refusals in the existing durable automation FIFO.

A producer ACK commits input, not inference. Resolve the current runtime before claiming that
input so an invalid config or unavailable credentials cannot settle a completion as an apology.
The FIFO is the outbox. Only its bounded backoff clock lives in state_meta, never a second copy
of the input. A restart resumes the same queued identity and its persisted deadline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from hermes_state_runtime import RuntimeStoreError, _admission, _epoch

logger = logging.getLogger(__name__)
_PREFIX = 'automation_refusal:'


def held_runtime_refusal(event, result):
    from gateway.session_results import execution_result
    captured = execution_result.get()
    if not (event.internal and isinstance(result, dict)
            and result.get('pre_execution_runtime_refusal') and captured is not None):
        return False
    captured['terminal_result'] = result
    return True


async def defer_claimed_refusal(authority, row, result):
    if not _automation(row):
        return
    from gateway.session_runtime_workers import tracked_write
    if not (isinstance(result, dict) and result.get('pre_execution_runtime_refusal')):
        # Clear only after execution, not on preflight success. Otherwise a repeated refusal
        # between preflight and execution would restart at 10s instead of growing its backoff.
        if await asyncio.to_thread(retry_state, authority.db, row['admission_id']):
            await tracked_write(authority, lambda: _clear_refusal(authority, row))
        return
    if result.get('api_calls') or result.get('tools') or result.get('messages'):
        raise AssertionError('only a proven pre-execution refusal may requeue an admission')
    await tracked_write(authority, lambda: _record_refusal(authority, row, claimed=True))
    raise AutomationRuntimeDeferred()



def _automation(row):
    payload = row.get('payload') or {}
    return ('local_automation_v1' in payload
            or 'automation' in (payload.get('native_text_v1') or {}))


def retry_state(db, admission_id):
    with db._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (_PREFIX + admission_id,)).fetchone()
    return json.loads(row[0]) if row else {}


class AutomationRuntimeDeferred(Exception):
    """The FIFO retained a pre-execution refusal, not an executed or unknown turn."""


def _record_refusal(authority, row, *, claimed=False):
    def write(conn):
        _epoch(conn, authority.epoch)
        current = _admission(conn, row['admission_id'])
        if claimed:
            if (current['status'] != 'started' or current['owner_epoch'] != authority.epoch
                    or current['generation'] != row['generation']):
                raise RuntimeStoreError('stale_generation')
            conn.execute("UPDATE session_admissions SET status='queued',owner_epoch=NULL,generation=NULL "
                         "WHERE admission_id=?", (row['admission_id'],))
        elif current['status'] != 'queued':
            raise RuntimeStoreError('stale_generation')
        old = conn.execute('SELECT value FROM state_meta WHERE key=?', (_PREFIX + row['admission_id'],)).fetchone()
        attempt = min(4, int(json.loads(old[0]).get('attempt', 0)) + 1) if old else 1
        state = {'attempt': attempt, 'due_at': time.time() + min(60.0, 10.0 * 2 ** (attempt - 1))}
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (_PREFIX + row['admission_id'], json.dumps(state)))
    authority.db._execute_write(write)


def _clear_refusal(authority, row):
    def write(conn):
        _epoch(conn, authority.epoch)
        conn.execute('DELETE FROM state_meta WHERE key=?', (_PREFIX + row['admission_id'],))
    authority.db._execute_write(write)


async def runtime_ready(authority, ref, row):
    """False leaves the same admission queued, with 10s, 20s, 40s, then at most 1 retry/minute.

    Never exhaust and drop a result during a long outage. Attempts and retained input are bounded,
    not outage duration. The normal owner/route checks still run before every eventual claim.
    """
    if not _automation(row):
        return True
    state = await asyncio.to_thread(retry_state, authority.db, row['admission_id'])
    if state.get('due_at', 0) > time.time():
        return False
    runner = authority.runner
    source = authority.sessions[ref.session_id].source
    home = None
    if 'native_text_v1' in row['payload']:
        from gateway.session_envelope import restore_native
        source = restore_native(row['payload'], runner).source
        provenance = row['payload']['native_text_v1'].get('provenance')
        if provenance is not None:
            from gateway.session_ingress_context import restore_provenance
            home = restore_provenance(runner, source, provenance)
    from gateway.session_ingress import _execution_scope
    from gateway.session_runtime_workers import tracked_write

    def resolve():
        from gateway.run import _load_gateway_runtime_config
        config = _load_gateway_runtime_config()
        runner._resolve_session_agent_runtime(source=source,
            session_key=authority.sessions[ref.session_id].route, user_config=config)

    try:
        with await _execution_scope(authority, home):
            await asyncio.to_thread(resolve)
    except AssertionError:
        raise  # Never turn an invariant or an isolated-home test guard failure into a held wake.
    except Exception:
        await tracked_write(authority, lambda: _record_refusal(authority, row))
        logger.warning('Automation admission %s retained queued after runtime refusal; retrying with backoff',
                       row['admission_id'], exc_info=True)
        return False
    return True
