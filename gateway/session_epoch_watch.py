"""Fail loudly when a live authority's runtime epoch is taken while this process still owns the store.

Every runtime write is fenced by ``runtime_epoch``: once it moves past an authority's epoch, every
settlement and admission that authority makes is refused with ``stale_epoch``, and only a restart
(which begins a new epoch and runs startup recovery) brings it back. ``begin_runtime_epoch`` refuses a
bump from a process that does not hold the profile lock, so a move while this process holds it means
a writer went around that check. Nothing else names it: users only see each turn fail. The
housekeeping tick runs this check, which logs CRITICAL once per taken epoch and publishes
``runtime_epoch: lost`` to the runtime status that readiness reports.

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


def lost_runtime_epochs(runner) -> list[dict]:
    """Each authority of *runner* whose store's epoch moved while this process owns that store."""
    from gateway.runtime_ownership import held_in_process
    from gateway.session_authorities import all_authorities
    lost = []
    for authority in all_authorities(runner):
        if authority is None or getattr(authority, 'retired', False):
            continue
        current = _store_epoch(authority.db)
        if current is None or current[0] == authority.epoch:
            continue
        home = Path(authority.db.db_path).resolve().parent
        if not held_in_process(home):
            continue  # ownership already released: this authority is shutting down, not robbed
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
