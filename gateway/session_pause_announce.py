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

The episode is the session's earliest ``unknown`` admission, and it outlives the process while
``pause_notified`` does not. A notice that reached the chat (sent here, or carried by a reply) is
recorded durably against that admission (``state_meta`` ``pause_notice:<admission_id>``), so a later
boot does not repeat it, while a boot whose send failed leaves it for the next boot to retry.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_TOLD = 'pause_notice:'
# The key range of every marker: ';' sorts right after ':'.
_TOLD_END = 'pause_notice;'
# Per session: the unprompted send in flight, resolved with whether it was delivered.
_SENDING = 'pause_notice_sends'
_UNKNOWN_HEAD = ("SELECT admission_id FROM session_admissions WHERE target_session_id=? AND status='unknown' "
                 'ORDER BY seq LIMIT 1')


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


async def await_announcement_send(authority, ref) -> None:
    """Wait out an unprompted notice already being sent to this chat. Its claim on
    ``pause_notified`` is not yet a delivered notice: a reply that saw it and stayed silent would
    leave the chat untold if the send then failed. Afterwards the flag says whether it got there."""
    from gateway.session_ingress import _registry
    sending = _registry(authority, _SENDING).get(ref.session_id)
    if sending is not None and not sending.done():
        await asyncio.shield(sending)


def _person_waiting(rows) -> bool:
    from gateway.session_unattended_unknown import is_unattended
    return any(row['status'] in ('queued', 'unknown') and not is_unattended(row) for row in rows)


def _owns_store(authority) -> bool:
    from gateway.session_authority import _stored_epoch
    return _stored_epoch(authority.db) == authority.epoch


def _already_told(db, session_id) -> bool:
    """This pause episode's notice already reached the chat, possibly from an earlier boot."""
    with db._read_ctx() as conn:
        head = conn.execute(_UNKNOWN_HEAD, (session_id,)).fetchone()
        return head is not None and conn.execute(
            'SELECT 1 FROM state_meta WHERE key=?', (_TOLD + head[0],)).fetchone() is not None


def _record_told(authority, session_id) -> None:
    """Mark the current episode told. Markers of earlier episodes, whose lost turn is no longer
    unknown, are dropped in the same write, so at most one per paused session remains."""
    from hermes_state_runtime import _epoch

    def write(conn):
        _epoch(conn, authority.epoch)
        conn.execute('DELETE FROM state_meta WHERE key >= ? AND key < ? AND NOT EXISTS ('
                     "SELECT 1 FROM session_admissions WHERE admission_id=substr(state_meta.key, ?) AND status='unknown')",
                     (_TOLD, _TOLD_END, len(_TOLD) + 1))
        head = conn.execute(_UNKNOWN_HEAD, (session_id,)).fetchone()
        if head is not None:
            conn.execute("INSERT OR REPLACE INTO state_meta(key,value) VALUES(?,'1')", (_TOLD + head[0],))
    authority.db._execute_write(write, session_ids=(session_id,))


async def remember_told(authority, ref) -> None:
    """Durably record that the chat was told about this pause (off the loop, never raising: a
    lost marker costs at most one repeated notice on a later boot)."""
    from gateway.session_runtime_workers import tracked_write
    try:
        await tracked_write(authority, lambda: _record_told(authority, ref.session_id))
    except Exception:
        logger.warning('Pause notice for session %s was sent but not recorded', ref.session_id, exc_info=True)


def schedule_remember_told(authority, ref):
    """``remember_told`` from a synchronous caller: the reply carrying the notice was delivered."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return None
    from gateway.session_runtime_workers import track_mutation
    return track_mutation(authority, remember_told(authority, ref))


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

    def due():
        """None: nobody to tell. False: an earlier notice for this episode already reached the chat."""
        if not (_owns_store(authority) and _person_waiting(
                list_session_admissions(authority.db, session_id=ref.session_id))):
            return None
        return not _already_told(authority.db, ref.session_id)
    verdict = await asyncio.to_thread(due)
    if not verdict:
        if verdict is False and authority.sessions.get(ref.session_id) is live:
            live.pause_notified = True  # the reply path must not repeat it either
        return False
    deliver = getattr(runner, '_delivery_adapter_for', None)
    adapter = deliver(source) if deliver is not None else None
    if adapter is None:
        return False
    if authority.sessions.get(ref.session_id) is not live:
        return False  # retired or replaced while the read ran
    # The adapter lookup awaited above. Recheck the durable human waiter and
    # episode immediately before claiming so a reply or discard that resolved
    # the pause during that await cannot send a stale notice.
    if not await asyncio.to_thread(due):
        return False
    text = pause_notice(authority, ref, reason)  # claims this episode's one notice
    if text is None:
        return False  # a message on the reply path already told the chat
    from gateway.run import _interim_metadata
    from gateway.session_ingress import _registry
    sends = _registry(authority, _SENDING)
    sending = sends[ref.session_id] = asyncio.get_running_loop().create_future()
    delivered = False
    try:
        result = await adapter.send(str(source.chat_id), text,
                                    metadata=_interim_metadata(runner._thread_metadata_for_source(source)))
        delivered = getattr(result, 'success', True) is not False
    except Exception:
        logger.warning('Pause notice for session %s could not be sent', ref.session_id, exc_info=True)
    finally:
        if not delivered:
            # Leave the episode unannounced: the next message's reply path still carries the notice.
            live.pause_notified = False
        if sends.get(ref.session_id) is sending:
            del sends[ref.session_id]
        sending.set_result(delivered)  # a reply that waited on this send claims it if it failed
    if not delivered:
        return False
    logger.info('Session %s paused (%s): notice sent to its chat', ref.session_id, reason)
    await remember_told(authority, ref)
    return True
