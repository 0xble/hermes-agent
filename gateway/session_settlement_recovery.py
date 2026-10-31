"""Fence an unsettled claim in a live owner without inventing a terminal result."""
import asyncio
import logging
import sqlite3

from hermes_state_runtime import RuntimeStoreError, _admission, _epoch, settle_steer_transfers

logger = logging.getLogger(__name__)


def _mark_unknown(authority, row):
    transfers = _transfers(authority, authority.sessions.get(row['target_session_id']), row)

    def write(conn):
        _epoch(conn, authority.epoch)
        current = _admission(conn, row['admission_id'])
        if current['owner_epoch'] != authority.epoch or current['generation'] != row['generation']:
            raise RuntimeStoreError('stale_generation')
        if current['status'] in {'terminal', 'unknown'}:
            return current['status']
        if current['status'] != 'started':
            raise RuntimeStoreError('stale_generation')
        conn.execute("UPDATE session_admissions SET status='unknown' WHERE admission_id=?", (row['admission_id'],))
        # A steer it carried is never replayed: interrupted if delivered, else queued again.
        settle_steer_transfers(conn, epoch=authority.epoch, generation=row['generation'],
                               transfers=transfers, outcome='unknown')
        conn.execute('UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?', (row['target_session_id'],))
        return 'unknown'
    return authority.db._execute_write(write)


def _transfers(authority, live, row):
    """The steers handed to this claim (``prompt.steer_queued``), read once its execution returned."""
    if live is None:
        return None
    from gateway.session_queued_controls import steer_transfers
    with live.event_stream.lock:
        return steer_transfers(authority, row['target_session_id'], row['generation'])


async def recover_failed_settlement(authority, row):
    # A full disk or held SQLite writer may also block the recovery stamp. Retain this owner
    # and its captured result until the storage fence can commit; never manufacture a reply.
    while True:
        try:
            from gateway.session_runtime_workers import track_mutation
            writer = track_mutation(authority, asyncio.to_thread(_mark_unknown, authority, row))
            return await asyncio.shield(writer)
        except (OSError, sqlite3.Error):
            await asyncio.sleep(1)


async def requeue_shutdown_unstarted(authority, ref, live, row):
    """A native turn this owner's restart or shutdown stopped before its first model call ran
    nothing: the claim goes back to its FIFO place for the successor, which runs it once, and this
    process sends nothing for it (no "send it again"). True when requeued; anything else settles
    as before. An API, LOCAL or managed claim has its own caller and receipts, so it never qualifies."""
    admission_id = row['admission_id']
    result = (authority.pending_results.get(admission_id) or {}).get('result') or {}
    from gateway.run_interrupt_cause import is_owner_shutdown_interrupt
    if not (authority.runner._draining and not authority.retiring and 'native_text_v1' in (row.get('payload') or {})
            and result.get('interrupted') and type(result.get('api_calls')) is int
            and result['api_calls'] == 0 and is_owner_shutdown_interrupt(result)):
        return False
    from gateway.session_ingress import deliver_settled, waiter_replies
    from gateway.session_queued_controls import publish_transfers
    from gateway.session_runtime_workers import track_mutation
    from hermes_state_runtime import requeue_unstarted_claim
    try:
        transfers = tuple(_transfers(authority, live, row) or ())
        requeued = await asyncio.shield(track_mutation(authority, asyncio.to_thread(
            requeue_unstarted_claim, authority.db, epoch=authority.epoch, admission_id=admission_id,
            generation=row['generation'], transfers=transfers, transcript_mark=row.get('transcript_mark'))))
    except (OSError, sqlite3.Error, RuntimeStoreError):
        logger.warning('Requeue of shutdown-interrupted admission %s failed; settling it', admission_id, exc_info=True)
        return False
    if not requeued:
        return False
    logger.info('Admission %s was stopped by shutdown before its first model call; requeued for the next owner',
                admission_id)
    authority.pending_results.pop(admission_id, None)
    authority.pending_deliveries.pop(admission_id, None)
    waiter_replies(authority).pop(admission_id, None)
    with live.event_stream.lock:
        live.event_stream.execution = {}
    for pending in (authority.pending_stops, authority.pending_stop_causes, authority.pending_steers, authority.adopted):
        pending.pop(ref.session_id, None)
    publish_transfers(authority, ref, row['generation'])
    authority.deferred_resets.pop(live.route, None)
    # Nothing was interrupted mid-work: the successor's replay is the resume, not a synthetic one.
    store = getattr(authority.runner, 'async_session_store', None)
    if store is not None:
        try:
            await store.clear_resume_pending(live.route)
        except Exception:
            logger.debug('clear_resume_pending after requeue failed for %s', live.route, exc_info=True)
    if authority.sessions.get(ref.session_id) is live:
        authority._publish_pending(ref)
    await deliver_settled(authority, admission_id)  # nothing to send: releases a held crash marker
    await authority._resolve_settled_waiter(admission_id, '')
    return True


def completion_payload(row, settled, response, captured):
    # ``status`` is the message.complete contract's TurnStatus: the Desktop
    # extends a Stopped bubble to the persisted partial only on 'interrupted'.
    complete = {
        'text': response, 'content': response, 'admission_id': row['admission_id'],
        'outcome': 'cancelled' if settled['outcome'] == 'interrupted' else settled['outcome'],
        'status': {'completed': 'complete', 'interrupted': 'interrupted'}.get(
            settled['outcome'], 'error')}
    # Only the agent's reuse site sets this (never inferred from equal text): the
    # final repeats a reply the viewer already painted, so it settles in place.
    captured_result = (captured or {}).get('result') or {}
    # A gateway-owned refusal brings its own descriptor (``session_cron.execute``); every other failed
    # turn gets the one the TUI path builds from the same result, so the viewer shows the right card.
    surface = captured_result.get('error_surface')
    if not isinstance(surface, dict) and complete['status'] == 'error':
        from agent.error_surface import build_error_surface_from_result
        surface = build_error_surface_from_result(
            captured_result, provider=str(captured_result.get('provider') or ''),
            model=str(captured_result.get('model') or ''))
    if isinstance(surface, dict):
        complete['error_surface'] = surface
    if response and captured_result.get('response_reused'):
        complete['response_reused'] = True
    if captured_result.get('local_viewer'):
        if captured_result.get('response_previewed'):
            complete['response_previewed'] = True
        if captured_result.get('response_transformed'):
            complete['response_transformed'] = True
        if isinstance(captured_result.get('warning'), str) and captured_result['warning']:
            complete['warning'] = captured_result['warning']
        if isinstance(captured_result.get('billing_block'), dict):
            complete['billing'] = captured_result['billing_block']
        if isinstance(captured_result.get('failure_reason'), str) and captured_result['failure_reason']:
            complete['failure_reason'] = captured_result['failure_reason']
        if isinstance(captured_result.get('last_reasoning'), str) and captured_result['last_reasoning'].strip():
            complete['reasoning'] = captured_result['last_reasoning'].strip()
    # The committed row addresses of the turn: a viewer binds the streamed reply to
    # its stored row, so a transcript read racing this frame never paints it twice.
    # ``submission_id`` names whose turn it is: the sending viewer binds its optimistic
    # prompt (``user-<submission_id>``), which the queued admission ack could not name.
    # A local viewer's turn end recorded the session-cumulative stats (``session_usage.publish_usage``):
    # main's TUI read ``message.complete.usage`` for its status bar and ``/usage`` totals.
    if isinstance((captured or {}).get('session_usage'), dict):
        from gateway.session_usage import tui_usage
        complete['usage'] = tui_usage(captured['session_usage'])
    if isinstance(captured_result.get('persisted_turn'), dict):
        complete['persisted_turn'] = {**captured_result['persisted_turn'],
                                      'submission_id': row['request_id']}
    return complete


def _publish_completion(authority, live, ref, row, settled, response, captured):
    """Thread-safe half of terminal publication; the caller holds the stream lock. No SQLite write
    happens here: loop-side readers wait on this lock, so it never spans storage I/O."""
    try:
        live.controls.snapshot(ref.session_id, None)
    except Exception:
        logger.exception('Terminal control cleanup failed for admission %s', row['admission_id'])
    complete = completion_payload(row, settled, response, captured)
    notice = owner_stop_notice(captured, settled['outcome'], row)
    if notice and not complete.get('warning'):
        # ``warning`` is the slot every surface already renders on message.complete (Ink activity
        # line, Desktop toast), so a turn the owner's stop cut short says why (#134889 F1).
        complete['warning'] = notice
    live.event_stream.publish(ref.session_id, complete)


RESTART_INTERRUPT_NOTICE = ('Gateway restarting: this turn was interrupted and will not be run again. '
                            'Send your message again once Hermes reconnects.')
SHUTDOWN_INTERRUPT_NOTICE = ('Gateway shutting down: this turn was interrupted and will not be run again. '
                             'Send your message again once Hermes is back.')


def owner_stop_notice(captured, outcome, row=None):
    """The named notice for a turn the owner's restart/stop interrupted, or None. With the default
    ``agent.restart_drain_timeout: 0`` a restart interrupts in-flight turns at once; the admission
    settles ``interrupted`` and is never replayed, so the viewer is told the restart did it.
    An unattended ``row`` (cron, automation, webhook) has nobody to resend it, so it gets the
    neutral automation copy instead of resend advice (#134889 GW-20, GW-10's rule)."""
    # The cause is the one the turn recorded when it was interrupted (stop() interrupts with these
    # reasons), never the runner's flags at publication: a user Stop whose settlement was delayed
    # until after shutdown began is still the user's Stop.
    from gateway.run import _INTERRUPT_REASON_GATEWAY_RESTART, _INTERRUPT_REASON_GATEWAY_SHUTDOWN
    cause = ((captured or {}).get('result') or {}).get('interrupt_message') if outcome == 'interrupted' else None
    notice = {_INTERRUPT_REASON_GATEWAY_RESTART: RESTART_INTERRUPT_NOTICE,
              _INTERRUPT_REASON_GATEWAY_SHUTDOWN: SHUTDOWN_INTERRUPT_NOTICE}.get(cause)
    if notice is not None and row is not None:
        from gateway.session_unattended_unknown import is_unattended
        if is_unattended(row):
            from agent.turn_failure_copy import AUTOMATION_INTERRUPTED_NOTICE
            return AUTOMATION_INTERRUPTED_NOTICE
    return notice


def gateway_refusal_boundary(captured, outcome):
    """``(notice, display_metadata)`` for a failed turn the gateway itself refused with a renderable
    descriptor (``session_cron.execute``: the only producer of ``result.error_surface``), else None."""
    value = (captured or {}).get('result') or {}
    surface, error = value.get('error_surface'), value.get('error')
    if outcome != 'failed' or not isinstance(surface, dict) or not isinstance(error, str) or not error:
        return None
    return error, {'error': error, 'error_surface': dict(surface)}


def commit_and_publish(authority, live, ref, row, response, outcome, captured, settlement):
    """Worker-thread settlement: redaction, encoding and the SQLite write never stall the owner loop.

    The write runs OUTSIDE ``event_stream.lock``: attach, replay, receipts and pending publication
    take that lock on the loop, and a writer held by another process (up to ``_WRITE_PATIENCE_S``)
    would stall every session behind it. Ordering is kept by ``live.settling``: from before the
    write until the completion frame is published, lock holders read this claim as still started,
    so none sees the terminal row without its completion. ``settlement`` receives the committed
    ``(settled, response)`` before publication, so a publish failure is not a lost commit.
    The caller publishes idle ``session.info`` on the loop afterwards (bot receipt wakeups are tasks).
    Terminal media cleanup is not part of it: ``settle_with_retry`` queues it once this returns.
    """
    from gateway.session_results import finish_result, prepare_result
    # Compaction, redaction and outcome flags are CPU-only.
    prepared = prepare_result(row, response, outcome, captured)
    admission_id = row['admission_id']
    # ``live`` is the drain's own entry: a delete committing on the loop after this write may
    # already have dropped it from ``authority.sessions``.
    with live.event_stream.lock:
        live.settling[admission_id] = row
    transfers = _transfers(authority, live, row)
    try:
        settlement['settled'], settlement['response'] = finish_result(
            authority.db, epoch=authority.epoch, row=row, response=response, outcome=outcome,
            result=captured, prepared=prepared, transfers=transfers)
        with live.event_stream.lock:
            live.settling.pop(admission_id, None)
            _publish_completion(authority, live, ref, row, settlement['settled'], settlement['response'], captured)
    finally:
        with live.event_stream.lock:
            live.settling.pop(admission_id, None)


# Pauses between settlement attempts after a transient storage error (held writer past its
# patience, full disk). Bounded: the attempts after the last one fall back to the unknown fence.
_SETTLE_RETRY_DELAYS_S = (0.5, 2.0)


async def settle_with_retry(authority, live, ref, row, response, outcome, captured, settlement):
    """Commit the captured result, retrying a transient storage failure a bounded number of times.

    One locked or full write must not turn a finished answer into an ``unknown`` turn whose result
    lives only in memory. Retrying is exact: a write that committed before reporting failure is
    read back by ``finish_result``, and a failure after the commit (``settled`` set) never retries.
    A fence refusal (``RuntimeStoreError``) is not transient and is raised at once."""
    from gateway.session_ingress_media import release_admission_media_later
    from gateway.session_runtime_workers import track_mutation
    for delay in (*_SETTLE_RETRY_DELAYS_S, None):
        try:
            await asyncio.shield(track_mutation(authority, asyncio.to_thread(
                commit_and_publish, authority, live, ref, row, response, outcome, captured, settlement)))
            # Queued, never awaited: the next FIFO row and this turn's delivery do not wait on it.
            release_admission_media_later(authority, row['admission_id'])
            return
        except (OSError, sqlite3.Error) as exc:
            if delay is None or 'settled' in settlement:
                raise
            logger.warning('Settlement of admission %s deferred by storage (%r); retrying in %.1fs',
                           row['admission_id'], exc, delay)
            await asyncio.sleep(delay)


def publish_terminal(authority, ref, row, settled, response, captured):
    """Cleanup cannot withhold a committed outcome; publication is attempted once."""
    live = authority.sessions[ref.session_id]
    with live.event_stream.lock:
        _publish_completion(authority, live, ref, row, settled, response, captured)
    from gateway.session_ingress_media import release_admission_media_later
    release_admission_media_later(authority, row['admission_id'])
    # Idle follows the completion so a viewer cannot mistake a settled turn for a lost frame.
    authority._publish_pending(ref)
