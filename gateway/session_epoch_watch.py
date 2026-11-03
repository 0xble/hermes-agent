"""Fail loudly when a live authority's runtime epoch is taken while this process still owns the store.

Every runtime write is fenced by ``runtime_epoch``: once it moves past an authority's epoch, every
settlement and admission that authority makes is refused with ``stale_epoch``, and only a restart
(which begins a new epoch and runs startup recovery) brings it back. ``begin_runtime_epoch`` refuses a
bump from a process that does not hold the profile lock, so a move while this process holds it means
a writer went around that check. Nothing else names it: users only see each turn fail. The
housekeeping tick runs this check, which logs CRITICAL once per taken epoch and publishes
``runtime_epoch: lost`` to the runtime status that readiness reports.

A zero-cut handover this gateway accepted is not a theft: its promoted sibling slot begins the next
epoch while this one finishes its in-flight turns. Only when the handover has not failed and the new
holder is the instance the sibling slot identifies as is that move logged at INFO instead. A sibling
too busy to answer stays pending for ``HANDOVER_PENDING_TICKS`` ticks before the move is reported.

It never re-claims the epoch. The process that took it may still be writing, so a re-claim would only
stale it in turn, and the recovery a begin runs would mark this owner's own running turns ``unknown``.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _store_epoch(db) -> tuple[int, str] | None:
    with db._read_ctx() as conn:
        row = conn.execute('SELECT epoch, instance_id FROM runtime_epoch WHERE singleton=1').fetchone()
    return (int(row[0]), str(row[1])) if row is not None else None


#: Ticks (one per housekeeping interval) an unanswered sibling ``identify`` stays pending during an
#: accepted handover before the move is reported as taken: a promoting slot can be too busy to
#: answer within one 0.5 s probe, and a CRITICAL "restart" mid-handover would cause the outage.
HANDOVER_PENDING_TICKS = 5

HANDED_OVER, NOT_HANDED_OVER, PENDING = "handed_over", "not_handed_over", "pending"


def _handover_verdict(runner, home, holder) -> str:
    """``handed_over`` when the epoch moved to the zero-cut successor this gateway handed over to:
    the handover was accepted and has not failed, and the new holder's instance id is the one the
    sibling slot publishes. ``pending`` when such a handover is under way but the sibling did not
    answer; ``not_handed_over`` otherwise (no handover, a failed one, or a different holder)."""
    state = getattr(runner, '_zero_cut_handover_state', None) or {}
    if not state.get('accepted') or state.get('failed') or state.get('status') == 'failed':
        return NOT_HANDED_OVER
    from gateway.control_socket import query_gateway_control
    from gateway.gateway_slot import SLOT_ACTIVE, SLOT_STANDBY, effective_gateway_slot
    sibling = SLOT_STANDBY if effective_gateway_slot() == SLOT_ACTIVE else SLOT_ACTIVE
    identity = query_gateway_control(home, "identify", slot=sibling, timeout=0.5)
    if not identity:
        return PENDING
    return HANDED_OVER if holder and identity.get('instance_id') == holder else NOT_HANDED_OVER


def lost_runtime_epochs(runner) -> list[dict]:
    """Each authority of *runner* whose store's epoch moved while this process owns that store."""
    from gateway.runtime_ownership import held_in_process
    from gateway.session_authorities import all_authorities
    from gateway.session_authority import UNCLAIMED_EPOCH
    lost = []
    for authority in all_authorities(runner):
        if authority is None or getattr(authority, 'retired', False) or authority.epoch == UNCLAIMED_EPOCH:
            continue
        current = _store_epoch(authority.db)
        if current is None or current[0] == authority.epoch:
            continue
        home = Path(authority.db.db_path).resolve().parent
        if not held_in_process(home):
            continue  # ownership already released: this authority is shutting down, not robbed
        if getattr(authority, '_epoch_handed_over', None) == current:
            continue  # this exact move was already proven to be the successor (asked once)
        verdict = _handover_verdict(runner, home, current[1])
        if verdict == HANDED_OVER:
            authority._epoch_handed_over = current
            logger.info('Runtime epoch of %s moved to %d (instance %r): the zero-cut successor '
                        'this gateway handed over to was promoted.', home, current[0], current[1])
            continue
        if verdict == PENDING:
            move, ticks = getattr(authority, '_epoch_handover_pending', (None, 0))
            ticks = ticks + 1 if move == current else 1
            authority._epoch_handover_pending = (current, ticks)
            if ticks < HANDOVER_PENDING_TICKS:
                if ticks == 1:
                    logger.info('Runtime epoch of %s moved to %d during an accepted handover; the '
                                'sibling slot did not answer yet, rechecking.', home, current[0])
                continue
        lost.append({'authority': authority, 'home': home, 'held': authority.epoch,
                     'store': current[0], 'holder': current[1]})
    return lost


def check_runtime_epochs(runner) -> list[dict]:
    """Housekeeping chore: CRITICAL once per taken epoch, and the runtime status on every tick."""
    lost = lost_runtime_epochs(runner)
    for item in lost:
        authority = item['authority']
        if getattr(authority, '_epoch_loss_reported', None) == item['store']:
            continue
        authority._epoch_loss_reported = item['store']
        logger.critical(
            'Runtime epoch of %s was taken by instance %r (store epoch %d, this owner holds %d) while '
            'this gateway still owns the profile: every turn now fails with stale_epoch. Find the '
            'process that wrote it, then restart the gateway.',
            item['home'], item['holder'], item['store'], item['held'])
    status = ({'status': 'lost', 'held': lost[0]['held'], 'store': lost[0]['store']} if lost
              else {'status': 'ok'})
    if getattr(runner, '_runtime_epoch_status', None) != status:
        try:
            from gateway.status import publish_runtime_status
            publish_runtime_status(runtime_epoch=status)
            runner._runtime_epoch_status = status
        except Exception:
            pass  # Runtime status is diagnostic; the CRITICAL line above already fired.
    return lost
