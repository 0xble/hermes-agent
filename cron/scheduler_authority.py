"""Transport-only cron execution; output/delivery remain with the firing scheduler."""
import asyncio
import hashlib
import json
import time
import uuid

# A submit normally answers in 30s; even two contended SQLite writes take 20s each.
# Leave room for ordered-admission queueing, and require repeated successful owner reads.
MISSING_SUBMIT_GRACE_SECONDS = 600
MISSING_SUBMIT_READS = 2


class CronExecutionUnknown(RuntimeError):
    """The owner may have accepted this fire; retain it until settlement or the missing fence."""


def journal_path(job_id, request_id):
    from hermes_constants import get_hermes_home
    key = hashlib.sha256(json.dumps([job_id, request_id]).encode()).hexdigest()
    return get_hermes_home() / 'cron' / 'admissions' / (key + '.json')


def _pause_identity(paused):
    """The exact pause this recovery uncertainty caused, so only that pause is ever lifted."""
    return {'paused_at': paused.get('paused_at'), 'reason': paused.get('paused_reason')} if paused else True


def replayable_completions(job_id, completions):
    """The recorded completion ids whose admission journal still exists: only journal recovery
    can replay a completion into ``mark_job_run``, so an id without one is a dead tombstone."""
    return [eid for eid in completions or () if journal_path(job_id, eid).exists()]


def pause_for_unknown(job_id, execution_id, reason):
    """The firer pauses the job for this fire's unverified admission and records that exact pause
    on the fire's journal, so the receipt that later proves the run terminal can lift it."""
    from cron.jobs import pause_job
    from utils import atomic_json_write
    from cron.jobs import _jobs_lock
    journal = journal_path(job_id, execution_id)
    with _jobs_lock():
        paused = pause_job(job_id, reason=reason)
        if paused and journal.exists():
            record = json.loads(journal.read_text(encoding='utf-8-sig'))
            record.setdefault('submit_started_at', journal.stat().st_mtime)
            record['pause_recorded'] = _pause_identity(paused)
            atomic_json_write(journal, record, mode=0o600, fsync_dir=True)


def _lift_pause(job_id, pause):
    """A verified terminal receipt resolved the uncertainty that paused the schedule: resume it iff
    the job is still held by exactly that pause (an operator's own pause/resume is never touched)."""
    from cron.jobs import _jobs_lock, get_job, resume_job
    with _jobs_lock():
        job = get_job(job_id) if isinstance(pause, dict) else None
        if (job is not None and job.get('state') == 'paused'
                and (job.get('paused_at'), job.get('paused_reason')) == (pause['paused_at'], pause['reason'])):
            try:
                resume_job(job_id)
            except ValueError:  # a one-shot whose instant has passed stays paused for the operator
                pass


def run_canonical_job(job, *, extra_prompt=None, cancel_event=None, execution_id=None):
    from gateway.session_cron import owner_for_home, operation
    from cron.scheduler_transport import recovering_cron_client
    from hermes_constants import get_hermes_home
    from utils import atomic_json_write
    from cron.jobs import _jobs_lock

    request_id = execution_id or job.get('execution_id')
    if not request_id:
        request_id = job['execution_id'] = uuid.uuid4().hex
    params = {'job_id': job['id'], 'request_id': request_id,
              'extra_prompt': extra_prompt}
    root = get_hermes_home() / 'cron' / 'admissions'
    journal = journal_path(params['job_id'], params['request_id'])
    record = {'params': params, 'receipt': None, 'job': dict(job), 'submit_started_at': time.time()}
    if journal.exists():
        record = json.loads(journal.read_text(encoding='utf-8-sig'))
        if record['params'] != params:
            raise CronExecutionUnknown('cron admission identity conflict')
    attempted = journal.exists()
    owner = owner_for_home(get_hermes_home())

    def save():
        # Share the jobs transaction lock with recovery's pause write and terminal retirement.
        # Atomic replace alone prevents torn JSON, not a lost read/merge/write update.
        with _jobs_lock():
            root.mkdir(parents=True, exist_ok=True)
            saved = json.loads(journal.read_text(encoding='utf-8-sig')) if journal.exists() else {}
            for key in ('pause_recorded', 'canonical_settled'):
                if key in saved:
                    record[key] = saved[key]
            atomic_json_write(journal, dict(saved, **record), mode=0o600, fsync_dir=True)

    async def observe(call):
        nonlocal attempted
        attempted = True
        save()
        try:
            receipt = await call('submit', params)
        except Exception as exc:
            from hermes_cli.gateway_client import GatewayRPCError
            # A returned owner exception is definite; a transport disconnect is not. Even an
            # owner error after commit must retain the admitted identity, so verify its absence.
            if owner is not None or isinstance(exc, GatewayRPCError):
                state = await call('recover', params)
                if state['status'] == 'missing':
                    error = f'{type(exc).__name__}: {exc}'
                    result = [False, f'# Cron Job: {job["id"]} (FAILED)\n\n{error}\n', '', error]
                    record['refusal'] = result
                    record['job'] = dict(job)
                    save()
                    return tuple(result)
                if state['status'] not in {'queued', 'started', 'terminal'}:
                    raise
                # Admitted before the answer was lost: observe it through the recovered receipt.
                receipt = {'session_id': state['session_id'], 'admission_id': state['admission_id']}
            else:
                raise
        record['receipt'] = receipt
        save()
        delay, progress = .1, None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                await call('cancel', receipt)
            state = await call('status', receipt)
            from cron.detached_authority import owns_detached_execution, run_detached
            if state['status'] == 'started' and owns_detached_execution(job['id'], request_id):
                # The admitted policy, not the firer's mutable snapshot, owns
                # execution inputs just as it does for gateway-hosted cron.
                state = await call('recover', params)
                if state['status'] != 'started':
                    continue
                value = await asyncio.to_thread(run_detached, dict(state['job']), receipt, state, record, save,
                    execution_id=request_id, extra_prompt=params['extra_prompt'], cancel_event=cancel_event)
                job.update(record['worker_result']['job_flags'])
                return value
            if state.get('progress_at') not in (None, progress):
                # The owner's agent is still working: refresh OUR ledger row (the owner's stamp is
                # fenced to the row's owner), once per owner stamp, so the stale-claim sweep measures
                # the run's silence. A wedged owner stops stamping and the sweep still fires.
                from cron.executions import touch_execution_progress
                progress = state['progress_at']
                await asyncio.to_thread(touch_execution_progress, request_id)
            if state['status'] == 'terminal':
                job.update(state.get('job_flags') or {})
                return tuple(state['result'])
            if state['status'] == 'unknown':
                raise CronExecutionUnknown('unknown_execution: cron admission was not replayed')
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2.0)

    async def remote():
        async with recovering_cron_client(params) as call:
            return await observe(call)

    try:
        if owner is not None:
            authority, loop = owner
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is loop:
                raise RuntimeError('cron synchronous execution must run off the owner event loop')
            return asyncio.run_coroutine_threadsafe(
                observe(lambda op, data: operation(authority, op, data)), loop).result()
        return asyncio.run(remote())
    except Exception as exc:
        if record.get('worker_result') is not None:
            return tuple(record['worker_result']['result'])
        if attempted:
            identity = f"job_id={job['id']} request_id={request_id} receipt={record.get('receipt')}"
            raise CronExecutionUnknown(
                f'Cron execution unverified; {identity}; reconcile {journal} '
                f'{type(exc).__name__}: {exc}') from exc
        error = f'{type(exc).__name__}: {exc}'
        return False, f'# Cron Job: {job["id"]} (FAILED)\n\n{error}\n', '', error

def _settled_now(journal):
    """Recovery stamps canonical_settled after import; re-read rather than trust the pre-recover copy."""
    try:
        return bool(json.loads(journal.read_text(encoding='utf-8-sig')).get('canonical_settled'))
    except (OSError, ValueError):
        return False


def _capture_unsettled(journal, record, pending, received):
    """True when the journal holds a capture that neither recovery nor the booked receipt imported."""
    if not pending or _settled_now(journal):
        return False
    captured = (record.get('worker_result') or {}).get('result')
    return json.dumps(captured) != json.dumps(received)


def _set_aside_unsettled(journal):
    """Move an unimported capture out of the replay glob, keeping its bytes."""
    import logging
    target = journal.with_name(journal.name + '.unsettled')
    journal.replace(target)
    logging.getLogger(__name__).warning(
        'cron journal %s kept as %s: terminal admission did not import its captured result',
        journal.name, target.name)


def retire_completed_journal(job_id, execution_id):
    """Lift only this run's uncertainty pause, in the same transaction that retires its link."""
    from cron.jobs import _jobs_lock

    journal = journal_path(job_id, execution_id)
    with _jobs_lock():
        record = json.loads(journal.read_text(encoding='utf-8-sig')) if journal.exists() else {}
        # A detached result still needs its SAME canonical admission to settle first.
        if 'worker_result' not in record or record.get('canonical_settled'):
            _lift_pause(job_id, record.get('pause_recorded'))
            journal.unlink(missing_ok=True)
            journal.with_suffix('.cancel').unlink(missing_ok=True)


def _observe_unverified(journal, state, current):
    """Bound a never-committed submit; a receipt/worker capture is never aged into failure."""
    from cron.jobs import _jobs_lock, pause_job
    from utils import atomic_json_write

    with _jobs_lock():
        record = json.loads(journal.read_text(encoding='utf-8-sig'))
        # Legacy journals have no start stamp. Preserve their original mtime before pause writes.
        started = record.setdefault('submit_started_at', journal.stat().st_mtime)
        record['missing_reads'] = record.get('missing_reads', 0) + 1 if state['status'] == 'missing' else 0
        if state.get('admission_id') and state.get('session_id'):
            record['receipt'] = {key: state[key] for key in ('session_id', 'admission_id')}
        if state['status'] in ('missing', 'unknown') and not record.get('pause_recorded'):
            reason = 'Canonical cron unverified; no automatic re-execution'
            record['pause_recorded'] = _pause_identity(pause_job(record['params']['job_id'], reason=reason))
        if (state['status'] == 'missing' and not record.get('receipt') and 'worker_result' not in record
                and time.time() - started >= MISSING_SUBMIT_GRACE_SECONDS
                and record['missing_reads'] >= MISSING_SUBMIT_READS and (record.get('job') or current)):
            error = (f'Submit never committed; verified by {MISSING_SUBMIT_GRACE_SECONDS}s grace '
                     f'and {record["missing_reads"]} consecutive missing owner reads')
            record['refusal'] = [False, '', '', error]
            record.setdefault('job', current)
            state = {'status': 'terminal', 'result': record['refusal'], 'job': record['job']}
        atomic_json_write(journal, record, mode=0o600, fsync_dir=True)
        return record, state


def _reset_missing_reads(journal):
    """A refused/failed read breaks the streak; it is not another absence observation."""
    from cron.jobs import _jobs_lock
    from utils import atomic_json_write

    with _jobs_lock():
        if journal.exists():
            record = json.loads(journal.read_text(encoding='utf-8-sig'))
            if record.get('missing_reads'):
                record['missing_reads'] = 0
                atomic_json_write(journal, record, mode=0o600, fsync_dir=True)


def reconcile_pending(*, allow_connect=True):
    """Observe prepared fires; never submit missing or interrupted work.

    With allow_connect=False, a headless tick leaves durable receipts for the next
    live owner instead of ensuring or spawning a gateway.
    """
    from gateway.session_cron import owner_for_home, operation
    from hermes_constants import get_hermes_home
    from hermes_cli.gateway_client import connect_gateway
    from cron.executions import execution_owner_live, get_execution
    from cron.jobs import get_job
    from cron.scheduler import (_RunDelivery, _FireOwnership, _apply_agent_failure_marker,
                                _save_compose_deliver)
    from cron.scheduler_bookkeeping import fail_empty_response, finish_completed_run
    import logging

    root = get_hermes_home() / 'cron' / 'admissions'
    for journal in sorted(root.glob('*.json')):
        try:
            record = json.loads(journal.read_text(encoding='utf-8-sig'))
            params = record['params']
            if journal != journal_path(params['job_id'], params['request_id']):
                raise ValueError('cron journal identity conflict')
            if execution_owner_live(params['request_id'], params['job_id']):
                # Its firer (this process included) still owns the row and its tail: re-saving the
                # output or re-marking the job on every tick would duplicate its side effects.
                continue
            row = get_execution(params['request_id'])
            terminal_execution = (row is not None and row['job_id'] == params['job_id']
                                  and row['status'] in {'completed', 'failed'})
            current = get_job(params['job_id'])
            booked_completion = row is None and params['request_id'] in (current or {}).get('canonical_completions', [])
            pending_canonical = 'worker_result' in record and not record.get('canonical_settled')
            if (terminal_execution or booked_completion) and not pending_canonical:
                retire_completed_journal(params['job_id'], params['request_id'])
                continue
            if 'refusal' in record:
                state = {'status': 'terminal', 'result': record['refusal'], 'job': record['job']}
            else:
                owner = owner_for_home(get_hermes_home())
                if owner is None and not allow_connect:
                    continue
                async def observe():
                    if owner is not None:
                        return await operation(owner[0], 'recover', params)
                    async with connect_gateway() as client:
                        return await client.rpc('cron.recover', **params)
                try:
                    if owner is not None:
                        state = asyncio.run_coroutine_threadsafe(observe(), owner[1]).result(timeout=20)
                    else:
                        state = asyncio.run(observe())
                except Exception:
                    _reset_missing_reads(journal)
                    raise
            if terminal_execution or booked_completion:
                # The worker already booked output/delivery, but the replacement
                # owner must import its captured result before deleting the only link.
                # A terminal admission is final even if its immutable result differs
                # from the capture and recover could not stamp canonical_settled.
                if state['status'] == 'terminal':
                    if _capture_unsettled(journal, record, pending_canonical, state.get('result')):
                        # The capture was never imported, so this journal holds its only
                        # copy. Retire it from replay without destroying that evidence.
                        _set_aside_unsettled(journal)
                    else:
                        journal.unlink(missing_ok=True)
                    journal.with_suffix('.cancel').unlink(missing_ok=True)
                continue
            if state['status'] != 'terminal':
                record, state = _observe_unverified(journal, state, current)
                if state['status'] != 'terminal':
                    continue
            received = state['result']
            success, output, answer, error = received
            job = dict(state['job'], **(state.get('job_flags') or {}))
            job['execution_id'] = params['request_id']
            # The same result verdicts as the ordinary tail: an agent-declared [CRON_FAILURE] and a
            # blank answer are failures, whichever process books them.
            success, error, declared = _apply_agent_failure_marker(job, success, error, answer)
            delivery = _RunDelivery(job, success, error, agent_declared=declared)
            # The canonical receipt supplies the result; a dead scheduler's fire claim does not
            # authorize replay and must not prevent finishing its durable bookkeeping.
            fence = _FireOwnership(dict(job, fire_claim=None))
            _save_compose_deliver(delivery, fence, answer, output if output.strip() else None, adapters=None, loop=None,
                                 verbose=False, execution_token=None)
            fail_empty_response(delivery, answer)
            if finish_completed_run(delivery, None, params['request_id'], recovered=True):
                # No execution row may remain to fence the next tick (legacy or
                # pruned runs). Successful recovery is itself terminal bookkeeping.
                if _capture_unsettled(journal, record, pending_canonical, received):
                    # Booked from a receipt that is not the capture: keep the capture's only copy.
                    _set_aside_unsettled(journal)
                else:
                    journal.unlink(missing_ok=True)
                journal.with_suffix('.cancel').unlink(missing_ok=True)
                _lift_pause(params['job_id'], record.get('pause_recorded'))
        except Exception:
            logging.getLogger(__name__).warning('Cron receipt recovery deferred: %s', journal, exc_info=True)
