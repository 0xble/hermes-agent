"""An ``unknown`` turn nobody is waiting on settles as interrupted (#134889 GW-10, #139532).

A turn lost across an owner restart is fenced ``unknown`` and pauses its session's FIFO, because
only the person who sent it knows whether to resend it. An automation delivery has no such person:
a cron run (``CRON_PRINCIPAL_ID``), a process completion or heartbeat (``automation:<route>``,
``local_automation_v1``, a native envelope's ``automation`` block) or a webhook delivery
(``webhook_delivery``), a Kanban card attempt (``kanban_card_v1``, set by the dispatcher),
or a trusted native synthetic producer descriptor without attended human activity.
Pausing on it stalls every message queued behind it until an operator notices
(a Bot Chat sat ten hours behind one).

Such a row settles through the same fenced resolution a person's Discard uses
(``resolve_unknown_session_input``): terminal ``interrupted``, never requeued or replayed, with a
Hermes transcript notice committed in the same transaction. No second recovery store.
"""
from __future__ import annotations

import logging

from hermes_state_keys import CRON_PRINCIPAL_ID

logger = logging.getLogger(__name__)


def _trusted_synthetic_descriptor(synthetic) -> bool:
    if not isinstance(synthetic, dict):
        return False
    if type(synthetic.get('internal')) is not bool:
        return False
    metadata = synthetic.get('metadata')
    if not isinstance(metadata, dict):
        return False
    origin = metadata.get('notification_origin')
    return (
        metadata.get('goal_continuation') is True
        or metadata.get('loop_wakeup') is True
        or metadata.get('hermes_loop_wakeup') is True
        or (isinstance(origin, str) and origin in {
            'process_registry_synthetic', 'delegation_result',
            'relay_delivery', 'cron_delivery',
        })
    )


def _synthetic_merge_veto(native) -> bool:
    if 'synthetic' not in native and 'absorbed_synthetic' not in native:
        return False
    # Receipts are incomplete: prefixes, edits, reactions and recovery may omit them.
    # Native ingress requires proof for the head AND every absorbed input before marker shortcuts.
    trail = native.get('absorbed_synthetic')
    return ('attended_activity' in native
            or not _trusted_synthetic_descriptor(native.get('synthetic'))
            or not isinstance(trail, list)
            or not all(_trusted_synthetic_descriptor(item) for item in trail))


def is_unattended(row) -> bool:
    """True for an admission no person submitted (so nobody can be asked to resend it)."""
    principal = row.get('principal_id') or ''
    payload = row.get('payload') or {}
    native = payload.get('native_text_v1') or {}
    if principal == CRON_PRINCIPAL_ID or principal.startswith('automation:'):
        # These producers never absorb native ingress merges. Legacy snapshots omitted
        # the empty trail, but an explicit human receipt or non-synthetic member still vetoes.
        trail = native.get('absorbed_synthetic', [])
        return ('attended_activity' not in native and isinstance(trail, list)
                and all(_trusted_synthetic_descriptor(item) for item in trail))
    if _synthetic_merge_veto(native):
        return False
    # A webhook delivery: the provider already got its 202 and nobody can be asked to resend it.
    # A Kanban card attempt (#134889 KB-8): the dispatcher's marker, committed only by
    # ``SessionAuthority.submit(_kanban_card=...)``; a card row main minted without it still pauses.
    return ('local_automation_v1' in payload or 'kanban_card_v1' in payload
            or 'automation' in native or 'webhook_delivery' in native
            or _trusted_synthetic_descriptor(native.get('synthetic')))


_NAME_CHARS = 80


def _quoted(text) -> str | None:
    """The first line of *text*, bounded, in quotes; None when empty."""
    line = (text or '').strip().splitlines()[0].strip() if (text or '').strip() else ''
    if not line:
        return None
    return f'"{line[:_NAME_CHARS - 1]}…"' if len(line) > _NAME_CHARS else f'"{line}"'


def _after(text: str, prefix: str) -> str | None:
    """The text after the first line starting with *prefix*, or None."""
    for line in (text or '').splitlines():
        if line.strip().startswith(prefix):
            return line.strip()[len(prefix):]
    return None


def _cron_job_name(job_id: str) -> str:
    try:
        from cron.jobs import get_job
        job = get_job(job_id) or {}
    except Exception:  # health: allow BLE001 -- best-effort label: the notice still names the job id
        job = {}
    return f'cron job {_quoted(job.get("name")) or job_id}'


def automation_name(row) -> str | None:
    """The interrupted automation in words the user recognizes (cron job, goal, /loop, process,
    delegation, relay note, webhook, Kanban card), or None when the admission does not say. Read
    from the producer's own request identity and prompt, never guessed."""
    import json

    request_id = str(row.get('request_id') or '')
    payload = row.get('payload') or {}
    text = payload.get('text') if isinstance(payload.get('text'), str) else ''
    if row.get('principal_id') == CRON_PRINCIPAL_ID and request_id.startswith('cron:'):
        return _cron_job_name(request_id.split(':')[1])
    try:
        identity = json.loads(request_id)
    except ValueError:
        identity = None
    kind = identity[0] if isinstance(identity, list) and identity and isinstance(identity[0], str) else ''
    subject = identity[1] if kind and len(identity) > 1 and isinstance(identity[1], str) else ''
    if kind == 'goal-wait':
        goal = _quoted(_after(text, 'Goal:') or _after(text, '[Continuing toward your standing goal] Goal:'))
        return f'the goal continuation for {goal}' if goal else 'a goal continuation'
    if kind == 'loop-tick':
        task = _quoted(_after(text, 'Recurring task:'))
        return f'the /loop wakeup for {task}' if task else 'a /loop wakeup'
    if kind == 'completion' and subject:
        command = _quoted(_after(text, 'Command:'))
        return f'the completion notice for background process {subject}' + (f' ({command})' if command else '')
    if kind in {'async_delegation', 'delegation_retry'} and subject:
        return f'the {"retry notice" if kind == "delegation_retry" else "result"} of delegation {subject}'
    if kind == 'plugin' and subject == 'session-relay':
        return 'a relayed inter-session note'
    native = payload.get('native_text_v1') or {}
    if 'webhook_delivery' in native:
        return 'a webhook delivery'
    if 'kanban_card_v1' in payload:
        return 'a Kanban card attempt'
    return None


def interrupted_notice(row) -> str:
    """The transcript notice for an unattended *row* settled interrupted, naming the automation."""
    from agent.turn_failure_copy import automation_interrupted_notice
    return automation_interrupted_notice(automation_name(row))


def _unattended_unknown(rows):
    return [row for row in rows if row['status'] == 'unknown' and is_unattended(row)]


def settle_recovered_unattended(db, epoch) -> int:
    """Boot recovery, right after ``recover_session_inputs`` fenced lost turns ``unknown`` and before
    any FIFO is scheduled: settle every unattended one. Synchronous; runs off the owner loop."""
    from gateway.session_ingress_media import release_admission_media
    from gateway.session_results import close_discarded_turn
    from hermes_state_runtime import RuntimeStoreError, _row, resolve_unknown_session_input
    with db._read_ctx() as conn:
        rows = [_row(r) for r in conn.execute(
            "SELECT * FROM session_admissions WHERE status='unknown' ORDER BY seq").fetchall()]
    settled = 0
    for row in _unattended_unknown(rows):
        from cron.detached_authority import recover_detached, defer_detached
        if recover_detached(db, epoch, row):
            settled += 1
            continue
        if defer_detached(db, row):
            continue
        try:
            notice = interrupted_notice(row)
            resolve_unknown_session_input(
                db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
                _terminal_write=lambda conn, lost, notice=notice: close_discarded_turn(db, conn, lost, notice))
        except RuntimeStoreError as exc:
            logger.warning('Lost automation admission %s left unknown: %s', row['admission_id'], exc.reason)
            continue
        release_admission_media(db, row['admission_id'])
        settled += 1
        logger.info('Lost automation admission %s on session %s settled interrupted (not replayed)',
                    row['admission_id'], row['target_session_id'])
    return settled


async def settle_unattended_unknown(authority, ref) -> bool:
    """The drain found an ``unknown`` head (fenced in this owner, e.g. a lost managed worker):
    settle the unattended ones through the owner's resolution. True when any settled, so the
    drain re-reads its FIFO instead of pausing; a person's ``unknown`` row still pauses it."""
    import asyncio

    from hermes_state_runtime import RuntimeStoreError, list_session_admissions
    rows = await asyncio.to_thread(list_session_admissions, authority.db, session_id=ref.session_id)
    settled = False
    for row in _unattended_unknown(rows):
        from cron.detached_authority import recover_detached, defer_detached
        if await asyncio.to_thread(recover_detached, authority.db, authority.epoch, row):
            settled = True
            continue
        if await asyncio.to_thread(defer_detached, authority.db, row):
            continue
        try:
            await authority.resolve_unknown_as_owner(ref, row['admission_id'], row['generation'],
                                                     notice=interrupted_notice(row))
        except RuntimeStoreError as exc:
            logger.warning('Lost automation admission %s left unknown: %s', row['admission_id'], exc.reason)
            continue
        settled = True
    return settled
