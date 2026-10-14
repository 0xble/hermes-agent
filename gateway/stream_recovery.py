"""Durable ownership of real streamed previews (not ephemeral draft frames).

Journal exact wire text before I/O and the platform message ID after ACK. A
provider stream has no completion proof until its terminal event: recovery
must not promote an arbitrary partial preview to a completed assistant turn.
Instead, auto-resume edits a known message; an ACK-lost first send gets an
honestly marked re-answer. Normal completion persists the real answer.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from gateway import delivery_ledger as ledger

logger = logging.getLogger(__name__)

# A contended state writer must never hold a stream edit behind the ledger's 10 s timeout.
_PREVIEW_BUSY_TIMEOUT_MS = 250
_PREVIEW_RETENTION_SECONDS = 86400 * 7
# State DB paths whose table this process has created and purged once (not per write).
_prepared_paths: set[str] = set()
_prepared_lock = threading.Lock()


def _prepare_journal(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS streamed_previews (
        turn_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, session_id TEXT NOT NULL,
        content TEXT NOT NULL, message_id TEXT, updated_at REAL NOT NULL
    )""")
    conn.execute("DELETE FROM streamed_previews WHERE updated_at < ?",
                 (time.time() - _PREVIEW_RETENTION_SECONDS,))


def _journal(op):
    """Run ``op(conn)`` in one commit-and-ALWAYS-close transaction (#69567: ``with conn:`` leaks fds).

    The table is created and the retention purge runs once per DB path per process, not on
    every stream edit; a table dropped underneath (profile purge) is re-prepared once.
    """
    import sqlite3

    from hermes_cli.sqlite_util import open_db, transaction

    path = str(ledger._db_path())
    for attempt in (0, 1):
        with _prepared_lock:
            prepare = path not in _prepared_paths
        try:
            with transaction(open_db(path, db_label="state.db (stream previews)", row_factory=None,
                                     busy_timeout_ms=_PREVIEW_BUSY_TIMEOUT_MS,
                                     initialize=_prepare_journal if prepare else None)) as conn:
                result = op(conn)
        except sqlite3.OperationalError as err:
            if attempt or prepare or "no such table" not in str(err):
                raise
            with _prepared_lock:
                _prepared_paths.discard(path)
            continue
        if prepare:
            with _prepared_lock:
                _prepared_paths.add(path)
        return result


def record_preview(*, turn_id, session_key, session_id, content, message_id):
    _journal(lambda conn: conn.execute("""INSERT INTO streamed_previews
        (turn_id, session_key, session_id, content, message_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(turn_id) DO UPDATE SET content=excluded.content,
            message_id=excluded.message_id, updated_at=excluded.updated_at""",
        (turn_id, session_key, session_id, content, message_id, time.time())))


def discard_preview(turn_id):
    _journal(lambda conn: conn.execute("DELETE FROM streamed_previews WHERE turn_id = ?", (turn_id,)))


def load_preview(session_key, session_id, turn_id):
    row = _journal(lambda conn: conn.execute("""SELECT content, message_id FROM streamed_previews
        WHERE turn_id = ? AND session_key = ? AND session_id = ?""",
        (turn_id, session_key, session_id)).fetchone())
    return {"content": row[0], "message_id": row[1]} if row else None


def _turn_event(runner, ctx):
    state = runner._peek_session_state(ctx.session_key) if ctx.session_key else None
    return getattr(getattr(state, "turn", None), "event", None)


def resume_admission_id(resume_turn_id) -> str:
    """Admission identity ``_schedule_resume_pending_sessions`` stamps on its synthetic event."""
    return f"resume:{resume_turn_id}"


def preview_recorder(runner, ctx):
    """Capture this admitted turn's private identity, never a successor's."""
    token = getattr(_turn_event(runner, ctx), "_gateway_active_turn_token", None)
    if not token or not ctx.session_id:
        return None

    async def record(content, message_id):
        if not content:
            await asyncio.to_thread(discard_preview, token)
            return
        await asyncio.to_thread(record_preview, turn_id=token, session_key=ctx.session_key,
                                session_id=ctx.session_id, content=content, message_id=message_id)
    return record


def restore_preview(runner, ctx, consumer, *, policy=None):
    """The synthetic auto-resume turn owns its predecessor's known editable preview.

    Only under ``restart_resume_policy: continue``: an ``ask`` turn's one-line question
    is not the interrupted answer, so the partial preview stays untouched and nothing is
    marked. Session-level ``resume_pending`` can outlive admission (breaker, stale marker,
    offline adapter), so a real inbound while it lingers must never adopt: the CURRENT
    turn must be the internal event admitted as ``resume:<resume_turn_id>``.
    Session ID and that exact token fence reset/successor ownership. An ACK-lost
    first send has no recoverable message ID and cannot be edited. Runs on the agent
    worker thread, not the gateway loop.
    """
    if policy != "continue" or not ctx.session_key or not ctx.session_id:
        return
    event = _turn_event(runner, ctx)
    if event is None or not getattr(event, "internal", False):
        return
    store = runner.session_store
    with store._lock:
        store._ensure_loaded_locked()
        entry = store._entries.get(ctx.session_key)
        token = getattr(entry, "resume_turn_id", None)
        if not token or not getattr(entry, "resume_pending", False) or entry.session_id != ctx.session_id:
            return
    if getattr(event, "_outbox_admission_id", None) != resume_admission_id(token):
        return
    preview = load_preview(ctx.session_key, ctx.session_id, token)
    if not preview:
        return
    # Re-own the row under this resumed turn's own active token: if it crashes too,
    # promotion makes that token the next resume_turn_id and recovery still finds it.
    successor = getattr(event, "_gateway_active_turn_token", None)
    if successor and successor != token:
        try:
            record_preview(turn_id=successor, session_key=ctx.session_key, session_id=ctx.session_id,
                           content=preview["content"], message_id=preview["message_id"])
            # Only once the successor owns it: a failed re-record keeps the predecessor row.
            discard_preview(token)
        except Exception:
            logger.warning("Could not re-own streamed preview for %s", ctx.session_key, exc_info=True)
    if not preview["message_id"]:
        # The platform may hold this send, but without its ACK there is no ID
        # to reconcile. Re-run the unfinished turn and label its completed reply;
        # buffering this recovery alone avoids a new unmarked preview beside it.
        consumer._recovered_send_uncertain = True
        consumer.cfg.buffer_only = True
        return
    consumer._adopt_message_id(preview["message_id"])
    consumer._last_sent_text = preview["content"]
    # The row is written BEFORE the edit it names, so the user may never have seen it:
    # until an edit lands, an identical re-answer is still edited onto the message and
    # this text never counts as visible or delivered.
    consumer._adopted_unconfirmed = consumer._message_id
    consumer._already_sent = True


def mark_recovered_reply(result, consumer):
    """Label delivery, not model history: return a delivered COPY whose final_response
    carries the marker, flagged ``recovered_reply`` so persistence strips it again.

    The caller's dict is never mutated. Never convert an interrupted/failed result or
    a journaled preview into a completed reply. Runs only after the resumed model call
    has returned.
    """
    if getattr(consumer, "_recovered_send_uncertain", False) is not True or not isinstance(result, dict):
        return result
    if result.get("failed") or result.get("interrupted") or result.get("completed") is False:
        return result
    text = result.get("final_response")
    if isinstance(text, str) and text.strip() and text != "(empty)" and not text.startswith(ledger.RECOVERED_MARKER):
        return {**result, "final_response": ledger.RECOVERED_MARKER + text, "recovered_reply": True}
    return result


def unmark_recovered_reply(text, agent_result):
    """The transcript keeps the model's answer, not the delivery label."""
    if (isinstance(agent_result, dict) and agent_result.get("recovered_reply") is True
            and isinstance(text, str)):
        return text.replace(ledger.RECOVERED_MARKER, "", 1)
    return text
