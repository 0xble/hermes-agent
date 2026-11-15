"""Tell a paused chat it is paused when no new message will (GW-10 follow-up).

``pause_notice`` reaches the platform user on the ordinary reply path: a message whose waiter the
pause released, or a new message admitted onto the paused FIFO. Startup recovery has neither. It
fences the lost turn ``unknown`` and pauses before any drain runs, and the person's messages queued
behind it were admitted by the previous owner, so nothing in this process waits on them. Without an
unprompted notice the chat stays silent until someone happens to write again.

``announce_pause`` sends that same generic notice once per pause episode (``LiveSession.pause_notified``,
shared with the reply path, so a later message does not repeat it) through the chat's normal delivery
adapter. It sends only while a person's input waits (an unattended head with nothing human behind it
pages nobody), never to a known bot, an app-only (LOCAL) session or a session with no deliverable
origin, and never from an owner that is draining or no longer holds the store's epoch.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def schedule_pause_announcement(authority, ref, reason):
    """Start (at most one per session) the unprompted notice for a pause no waiter reports."""
    if reason != 'unknown_execution':
        return None
    from gateway.session_ingress import _registry
    tasks = _registry(authority, 'pause_announcements')
    running = tasks.get(ref.session_id)
    if running is not None and not running.done():
        return running
    try:
        task = asyncio.get_running_loop().create_task(announce_pause(authority, ref, reason))
    except RuntimeError:
        return None  # no loop (a synchronous caller): the reply path still notices the next message
    tasks[ref.session_id] = task
    task.add_done_callback(lambda done: tasks.pop(ref.session_id, None) if tasks.get(ref.session_id) is done else None)
    return task


def _person_waiting(rows) -> bool:
    from gateway.session_unattended_unknown import is_unattended
    return any(row['status'] in ('queued', 'unknown') and not is_unattended(row) for row in rows)


def _owns_store(authority) -> bool:
    from gateway.session_authority import _stored_epoch
    return _stored_epoch(authority.db) == authority.epoch


async def announce_pause(authority, ref, reason) -> bool:
    """True when the notice was delivered to the chat."""
    from gateway.bot_peer_notices import automatic_notices_allowed
    from gateway.config import Platform
    from gateway.session_ingress import pause_notice
    from hermes_state_runtime import list_session_admissions
    runner = authority.runner
    live = authority.sessions.get(ref.session_id)
    if live is None or live.pause_notified or getattr(runner, '_draining', False):
        return False
    source = live.source
    if (source is None or getattr(source, 'chat_id', None) is None
            or getattr(source, 'platform', None) == Platform.LOCAL or not automatic_notices_allowed(source)):
        return False

    def waiting():
        return _owns_store(authority) and _person_waiting(
            list_session_admissions(authority.db, session_id=ref.session_id))
    if not await asyncio.to_thread(waiting):
        return False
    deliver = getattr(runner, '_delivery_adapter_for', None)
    adapter = deliver(source) if deliver is not None else None
    if adapter is None:
        return False
    if authority.sessions.get(ref.session_id) is not live:
        return False  # retired or replaced while the read ran
    text = pause_notice(authority, ref, reason)  # claims this episode's one notice
    if text is None:
        return False  # a message on the reply path already told the chat
    from gateway.run import _interim_metadata
    delivered = False
    try:
        result = await adapter.send(str(source.chat_id), text,
                                    metadata=_interim_metadata(runner._thread_metadata_for_source(source)))
        delivered = getattr(result, 'success', True) is not False
    except Exception:
        logger.warning('Pause notice for session %s could not be sent', ref.session_id, exc_info=True)
    if not delivered:
        # Leave the episode unannounced: the next message's reply path still carries the notice.
        live.pause_notified = False
        return False
    logger.info('Session %s paused (%s): notice sent to its chat', ref.session_id, reason)
    return True
