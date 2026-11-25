"""ACP v0.9 has no per-session destroy: the last viewer leaving an idle ACP session ends it (#118216).

The in-process adapter stamped ``ended_at`` at its stdio shutdown; under the gateway the ACP process
is only a viewer, so the owner stamps it when the last subscription goes or the gateway stops. Without
a writer, source='acp' rows stay open forever and the ended-session guard keeps prune/archive away.
A later ``session/load`` + prompt reopens the row (``session_local_recovery.reopen_local_session``).
"""
import asyncio
from functools import partial
import logging
from threading import Event

from gateway.session_persistence import authority_persistence
from persistence.core.errors import AuthorityRefusal

ACP_END_REASON = 'acp_disconnect'


def end_idle_acp_session(authority, session_id):
    """End *session_id* iff it is an ACP-created local session with no viewer and an idle FIFO."""
    live = authority.sessions.get(session_id)
    if live is not None and live.subscribers:
        return False
    store = authority_persistence(authority)
    try:
        receipt = store.bindings.receipt(session_id)
    except AuthorityRefusal:
        return False  # not a local session: no ACP policy to end
    if receipt.policy.get('source') != 'acp':
        return False
    try:
        return bool(store.bindings.end_idle(epoch=authority.epoch, session_id=session_id,
                                            target_id=receipt.entry['session_id'], reason=ACP_END_REASON))
    except (AuthorityRefusal, KeyError, TypeError):
        logging.getLogger(__name__).warning('ACP session %s not ended', session_id, exc_info=True)
        return False


def end_idle_viewer_session(authority, session_id, reason):
    """``session.detach {end_reason}`` from the last viewer: end *session_id*'s idle local row with the
    viewer's own reason (main's classic CLI ``end_session(sid, 'cli_close')`` and its switch reasons).
    ACP rows keep their own reason; a busy FIFO keeps the row open, as for ACP."""
    live = authority.sessions.get(session_id)
    if live is not None and live.subscribers:
        return False
    store = authority_persistence(authority)
    try:
        receipt = store.bindings.receipt(session_id)
    except AuthorityRefusal:
        return False  # not a local session: nothing this verb may end
    if receipt.policy.get('source') == 'acp':
        return end_idle_acp_session(authority, session_id)
    try:
        return bool(store.bindings.end_idle(epoch=authority.epoch, session_id=session_id,
                                            target_id=receipt.entry['session_id'], reason=reason))
    except (AuthorityRefusal, KeyError, TypeError):
        logging.getLogger(__name__).warning('local session %s not ended', session_id, exc_info=True)
        return False


async def reopen_if_reattached(authority, session_id, ended):
    """On the owner loop after the close committed: a viewer attached meanwhile, so the session
    it watches is live again (the stamp would make active work prune/archive eligible)."""
    live = authority.sessions.get(session_id)
    if ended and live is not None and live.subscribers:
        import asyncio
        store = authority_persistence(authority)
        target = store.bindings.receipt(session_id).entry['session_id']
        await asyncio.to_thread(store.sessions.reopen, target)
    return ended


def _end_idle_acp_sessions(authority, stopped):
    """Gateway shutdown: every ACP session nobody is viewing, attached this run or not."""
    if stopped.is_set():
        return 0
    ids = authority_persistence(authority).bindings.local_session_ids()
    ended = 0
    for session_id in ids:
        if stopped.is_set():
            break
        ended += end_idle_acp_session(authority, session_id)
    return ended


async def end_idle_acp_sessions(authority, deadline):
    """Close unviewed ACP sessions within shutdown's remaining settle budget."""
    from gateway.session_runtime_workers import tracked_write

    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return 0
    stopped = Event()
    try:
        close = partial(_end_idle_acp_sessions, authority, stopped)
        return await asyncio.wait_for(tracked_write(authority, close), remaining)
    finally:
        # A waiting SQLite call may still be running. It retains authority ownership,
        # but it must not start closing more sessions after the deadline or cancellation.
        stopped.set()
