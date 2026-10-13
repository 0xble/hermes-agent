"""Durable ownership of real streamed previews (not ephemeral draft frames).

Journal exact wire text before I/O and the platform message ID after ACK. A
provider stream has no completion proof until its terminal event: recovery
must not promote an arbitrary partial preview to a completed assistant turn.
Instead, auto-resume edits a known message; an ACK-lost first send gets an
honestly marked re-answer. Normal completion persists the real answer.
"""
from __future__ import annotations

import asyncio
import time

from gateway import delivery_ledger as ledger


def _ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS streamed_previews (
        turn_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, session_id TEXT NOT NULL,
        content TEXT NOT NULL, message_id TEXT, updated_at REAL NOT NULL
    )""")


def record_preview(*, turn_id, session_key, session_id, content, message_id):
    with ledger._connect() as conn:
        _ensure_table(conn)
        conn.execute("""INSERT INTO streamed_previews
            (turn_id, session_key, session_id, content, message_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(turn_id) DO UPDATE SET content=excluded.content,
                message_id=excluded.message_id, updated_at=excluded.updated_at""",
            (turn_id, session_key, session_id, content, message_id, time.time()))
        conn.execute("DELETE FROM streamed_previews WHERE updated_at < ?", (time.time() - 86400 * 7,))


def discard_preview(turn_id):
    with ledger._connect() as conn:
        _ensure_table(conn)
        conn.execute("DELETE FROM streamed_previews WHERE turn_id = ?", (turn_id,))


def load_preview(session_key, session_id, turn_id):
    with ledger._connect() as conn:
        _ensure_table(conn)
        row = conn.execute("""SELECT content, message_id FROM streamed_previews
            WHERE turn_id = ? AND session_key = ? AND session_id = ?""",
            (turn_id, session_key, session_id)).fetchone()
    return {"content": row[0], "message_id": row[1]} if row else None


def preview_recorder(runner, ctx):
    """Capture this admitted turn's private identity, never a successor's."""
    state = runner._peek_session_state(ctx.session_key) if ctx.session_key else None
    event = getattr(getattr(state, "turn", None), "event", None)
    token = getattr(event, "_gateway_active_turn_token", None)
    if not token or not ctx.session_id:
        return None

    async def record(content, message_id):
        if not content:
            await asyncio.to_thread(discard_preview, token)
            return
        await asyncio.to_thread(record_preview, turn_id=token, session_key=ctx.session_key,
                                session_id=ctx.session_id, content=content, message_id=message_id)
    return record


def restore_preview(runner, ctx, consumer):
    """A resumed stream owns its predecessor's known editable preview.

    Reading happens on the existing agent worker thread, not the gateway loop.
    Session ID and the exact resume-turn token fence reset/successor ownership.
    An ACK-lost first send has no recoverable message ID and cannot be edited.
    """
    if not ctx.session_key or not ctx.session_id:
        return
    store = runner.session_store
    with store._lock:
        store._ensure_loaded_locked()
        entry = store._entries.get(ctx.session_key)
        token = getattr(entry, "resume_turn_id", None)
        if not token or not getattr(entry, "resume_pending", False) or entry.session_id != ctx.session_id:
            return
    preview = load_preview(ctx.session_key, ctx.session_id, token)
    if not preview:
        return
    if not preview["message_id"]:
        # The platform may hold this send, but without its ACK there is no ID
        # to reconcile. Re-run the unfinished turn and label its completed reply;
        # buffering this recovery alone avoids a new unmarked preview beside it.
        consumer._recovered_send_uncertain = True
        consumer.cfg.buffer_only = True
        return
    consumer._adopt_message_id(preview["message_id"])
    consumer._last_sent_text = preview["content"]
    consumer._already_sent = True
    consumer._first_message_sent_at = time.monotonic()


def mark_recovered_reply(result, consumer):
    """Label delivery, not model history: normal persistence keeps the real re-answer.

    Never convert an interrupted/failed result or a journaled preview into a
    completed reply. This runs only after the resumed model call has returned.
    """
    if getattr(consumer, "_recovered_send_uncertain", False) is not True or not isinstance(result, dict):
        return
    if result.get("failed") or result.get("interrupted") or result.get("completed") is False:
        return
    text = result.get("final_response")
    if isinstance(text, str) and text.strip() and text != "(empty)" and not text.startswith(ledger.RECOVERED_MARKER):
        result["final_response"] = ledger.RECOVERED_MARKER + text
