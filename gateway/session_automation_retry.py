"""Retry proven pre-execution refusals in the existing durable automation FIFO.

A producer ACK commits input, not inference. Only the actual executor can prove a runtime
refusal: admission must not resolve a model for notes or a separately configured worker.
The FIFO is the outbox. Only its bounded backoff clock lives in state_meta, never a second copy
of the input. A restart resumes the same queued identity and its persisted deadline.
"""
from __future__ import annotations

import asyncio
import json
import time

from hermes_state_runtime import RuntimeStoreError, _admission, _epoch

_PREFIX = 'automation_refusal:'


def held_runtime_refusal(event, result):
    from gateway.session_results import execution_result
    captured = execution_result.get()
    if not (event.internal and isinstance(result, dict)
            and result.get('pre_execution_runtime_refusal') and captured is not None):
        return False
    captured['terminal_result'] = result
    return True


async def defer_claimed_refusal(authority, ref, row, result):
    if not is_automation(row):
        return
    from gateway.session_runtime_workers import tracked_write
    if not (isinstance(result, dict) and result.get('pre_execution_runtime_refusal')):
        # A claim does not prove execution. Keep the attempt count until the executor
        # succeeds, so repeated refusals grow the same admission's backoff.
        if await asyncio.to_thread(retry_state, authority.db, row['admission_id']):
            await tracked_write(authority, lambda: _clear_refusal(authority, row))
        return
    if result.get('api_calls') or result.get('tools') or result.get('messages'):
        raise AssertionError('only a proven pre-execution refusal may requeue an admission')
    if not await tracked_write(authority, lambda: _record_refusal(authority, ref, row)):
        # A Stop this claim accepted ends the work: it settles interrupted, never retried later
        # when the model or credentials recover.
        result.pop('failed', None)
        result.update(interrupted=True, completed=False)
        return
    raise AutomationRuntimeDeferred()


def _stop_accepted(authority, ref, row):
    """``interrupt`` accepted a Stop for this exact claim: latched before an agent existed, or
    delivered to the route's agent. Both are loop-side dicts; a read here is atomic."""
    generation = row['generation']
    return generation in (authority.pending_stops.get(ref.session_id),
                          authority.delivered_stops.get(ref.session_id))


def is_automation(row):
    payload = row.get('payload') or {}
    return ('local_automation_v1' in payload
            or 'automation' in (payload.get('native_text_v1') or {}))


def refusal_held(conn, admission_id):
    """True inside a write when this queued admission carries a proven refusal's backoff."""
    return conn.execute('SELECT 1 FROM state_meta WHERE key=?', (_PREFIX + admission_id,)).fetchone() is not None


def retry_state(db, admission_id):
    with db._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (_PREFIX + admission_id,)).fetchone()
    return json.loads(row[0]) if row else {}


class AutomationRuntimeDeferred(Exception):
    """The FIFO retained a pre-execution refusal, not an executed or unknown turn."""


def _record_refusal(authority, ref, row):
    """Requeue the refused claim with its next backoff: True. False, writing only the cleared
    retry clock, when a Stop was accepted for the claim; it is checked inside the write, so a Stop
    that lands while the write waits for the writer lock is honored too."""
    def write(conn):
        _epoch(conn, authority.epoch)
        current = _admission(conn, row['admission_id'])
        if (current['status'] != 'started' or current['owner_epoch'] != authority.epoch
                or current['generation'] != row['generation']):
            raise RuntimeStoreError('stale_generation')
        if _stop_accepted(authority, ref, row):
            conn.execute('DELETE FROM state_meta WHERE key=?', (_PREFIX + row['admission_id'],))
            return False
        conn.execute("UPDATE session_admissions SET status='queued',owner_epoch=NULL,generation=NULL "
                     "WHERE admission_id=?", (row['admission_id'],))
        old = conn.execute('SELECT value FROM state_meta WHERE key=?', (_PREFIX + row['admission_id'],)).fetchone()
        attempt = min(4, int(json.loads(old[0]).get('attempt', 0)) + 1) if old else 1
        state = {'attempt': attempt, 'due_at': time.time() + min(60.0, 10.0 * 2 ** (attempt - 1))}
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (_PREFIX + row['admission_id'], json.dumps(state)))
        return True
    return authority.db._execute_write(write)


def _clear_refusal(authority, row):
    def write(conn):
        _epoch(conn, authority.epoch)
        conn.execute('DELETE FROM state_meta WHERE key=?', (_PREFIX + row['admission_id'],))
    authority.db._execute_write(write)


async def claimable_head(authority, queued):
    """``(row, passing)``: the queued row the next claim validates and the ids it may pass.

    Normally the FIFO head. An automation head waiting out a proven refusal's backoff yields
    to the first person's input queued behind it: a refusal that persists (a broken model or
    credential route) must not hold human turns behind it forever, and the human turn reports
    that failure itself. Later automation rows keep their place behind the refused one.
    """
    if not queued:
        return None, ()
    head = queued[0]
    if await retry_ready(authority, head):
        return head, ()
    for index, row in enumerate(queued[1:], 1):
        if not is_automation(row):
            return row, tuple(item['admission_id'] for item in queued[:index])
    return head, ()


async def retry_ready(authority, row):
    """Honor a proven refusal's deadline without probing the executor's model or credentials.

    Initial admissions, notes and managed-worker handoffs must reach their own executor.
    The executor's explicit refusal marker is the only evidence safe enough to requeue.
    Never exhaust and drop a result during a long outage. The normal owner/route checks
    still run before every eventual claim.
    """
    if not is_automation(row):
        return True
    state = await asyncio.to_thread(retry_state, authority.db, row['admission_id'])
    return state.get('due_at', 0) <= time.time()
