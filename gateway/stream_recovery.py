"""Durable ownership of real streamed previews (not ephemeral draft frames).

Journal exact wire text before I/O and the platform message ID after ACK. A
provider stream has no completion proof until its terminal event: recovery
must not promote an arbitrary partial preview to a completed assistant turn.
Instead, auto-resume edits a known message; an ACK-lost first send gets an
honestly marked re-answer. Normal completion persists the real answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from gateway import delivery_ledger as ledger

logger = logging.getLogger(__name__)

# A contended state writer must never hold a stream edit behind the ledger's 10 s timeout.
_PREVIEW_BUSY_TIMEOUT_MS = 250
# The resume re-own runs once per recovery on the worker thread, off the edit hot path,
# so it may wait as long as the delivery ledger does instead of losing ownership to contention.
_REOWN_BUSY_TIMEOUT_MS = 10_000
_PREVIEW_RETENTION_SECONDS = 86400 * 7
# State DB paths whose table this process has created and purged once (not per write).
_prepared_paths: set[str] = set()
_prepared_lock = threading.Lock()
_SPLIT_PREVIEW_PREFIX = "\x00hermes-split-preview:"
# Strong refs for cleanup scheduled on an already-running loop (bare tasks can be GC'd).
_cleanup_tasks: set = set()


def _finish_foreign_cleanup(future, label: str) -> None:
    """Release and log cleanup that was submitted to the owning loop from another loop."""
    _cleanup_tasks.discard(future)
    if future.cancelled():
        logger.debug("%s: cross-loop cleanup cancelled", label)
    elif future.exception() is not None:
        logger.warning("%s: cross-loop cleanup failed: %s", label, future.exception())


def _delete_recovered_previews(ctx, delete, message_ids, *, label: str) -> None:
    """Run adapter cleanup on the gateway loop that owns its async transport."""
    loop = getattr(ctx, "_loop_for_step", None)
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is not None and (loop is None or current is loop):
        # On the owning loop itself: blocking here would deadlock it and asyncio.run
        # would raise, so schedule best-effort cleanup.
        logger.debug("%s: scheduling cleanup on the running loop", label)
        task = current.create_task(delete(message_ids, label=label, skip_sentinel=False))
        _cleanup_tasks.add(task)
        task.add_done_callback(_cleanup_tasks.discard)
        return
    if current is not None and loop is not None:
        # On a foreign running loop: the transport belongs to ``loop``, so submit there
        # without blocking (``.result()`` on a running loop can deadlock).
        logger.debug("%s: submitting cleanup to the owning loop", label)
        future = asyncio.run_coroutine_threadsafe(
            delete(message_ids, label=label, skip_sentinel=False), loop,
        )
        _cleanup_tasks.add(future)
        future.add_done_callback(lambda f: _finish_foreign_cleanup(f, label))
        return
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(
            delete(message_ids, label=label, skip_sentinel=False), loop,
        ).result(timeout=10)
        return
    # No owning loop is running: a throwaway loop would drive a transport bound elsewhere, so skip.
    logger.warning("%s: no running gateway loop; skipping recovered preview cleanup", label)


_REPLACEMENT_PREVIEW_PREFIX = "\x00hermes-replacement-preview:"


def _prepare_journal(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS streamed_previews (
        turn_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, session_id TEXT NOT NULL,
        content TEXT NOT NULL, message_id TEXT, updated_at REAL NOT NULL
    )""")
    conn.execute("DELETE FROM streamed_previews WHERE updated_at < ?",
                 (time.time() - _PREVIEW_RETENTION_SECONDS,))


def _journal(op, *, busy_timeout_ms=_PREVIEW_BUSY_TIMEOUT_MS):
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
                                     busy_timeout_ms=busy_timeout_ms,
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


def _redact_preview_content(content):
    """Redact preview text before it enters the crash journal.

    Replacement and split rows carry control prefixes plus JSON recovery metadata. Redacting
    those rows as one string could alter an ID or make the marker unparsable, so redact only the
    replacement's visible text and preserve the recovery metadata byte-for-byte. Ordinary preview
    text follows the same storage redactor as assistant transcript content.
    """
    if not isinstance(content, str) or not content:
        return content
    from agent.redact import redact_sensitive_text

    if content.startswith(_SPLIT_PREVIEW_PREFIX):
        try:
            message_ids = json.loads(content[len(_SPLIT_PREVIEW_PREFIX):])
        except (TypeError, ValueError):
            return redact_sensitive_text(content)
        if not isinstance(message_ids, list):
            return redact_sensitive_text(content)
        return _SPLIT_PREVIEW_PREFIX + json.dumps(message_ids)

    if content.startswith(_REPLACEMENT_PREVIEW_PREFIX):
        try:
            intent = json.loads(content[len(_REPLACEMENT_PREVIEW_PREFIX):])
        except (TypeError, ValueError):
            return redact_sensitive_text(content)
        if not isinstance(intent, dict):
            return redact_sensitive_text(content)
        redacted = dict(intent)
        if isinstance(redacted.get("text"), str):
            redacted["text"] = redact_sensitive_text(redacted["text"])
        return _REPLACEMENT_PREVIEW_PREFIX + json.dumps(redacted)

    return redact_sensitive_text(content)


def record_preview(*, turn_id, session_key, session_id, content, message_id):
    content = _redact_preview_content(content)
    _journal(lambda conn: conn.execute("""INSERT INTO streamed_previews
        (turn_id, session_key, session_id, content, message_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(turn_id) DO UPDATE SET content=excluded.content,
            message_id=excluded.message_id, updated_at=excluded.updated_at""",
        (turn_id, session_key, session_id, content, message_id, time.time())))


def discard_preview(turn_id):
    _journal(lambda conn: conn.execute("DELETE FROM streamed_previews WHERE turn_id = ?", (turn_id,)))


def record_split_preview(*, turn_id, session_key, session_id, message_ids):
    """Durably fence a split-send window until its sealed heads are retired.

    The marker is deliberately kept in the same preview table: recovery can see
    that accepted head messages exist, but must never adopt one as the editable
    live preview.
    """
    payload = _SPLIT_PREVIEW_PREFIX + json.dumps([str(mid) for mid in message_ids if mid])
    _journal(lambda conn: conn.execute("""INSERT INTO streamed_previews
        (turn_id, session_key, session_id, content, message_id, updated_at)
        VALUES (?, ?, ?, ?, NULL, ?)
        ON CONFLICT(turn_id) DO UPDATE SET content=excluded.content,
            message_id=NULL, updated_at=excluded.updated_at""",
        (turn_id, session_key, session_id, payload, time.time())))


def load_preview(session_key, session_id, turn_id):
    row = _load_preview_row(session_key, session_id, turn_id)
    return {"content": row["content"], "message_id": row["message_id"]} if row else None


def _load_preview_row(session_key, session_id, turn_id):
    row = _journal(lambda conn: conn.execute("""SELECT turn_id, content, message_id FROM streamed_previews
        WHERE turn_id = ? AND session_key = ? AND session_id = ?""",
        (turn_id, session_key, session_id)).fetchone())
    return {"turn_id": row[0], "content": row[1], "message_id": row[2]} if row else None


def _load_session_preview_rows(session_key, session_id):
    rows = _journal(lambda conn: conn.execute("""SELECT turn_id, content, message_id FROM streamed_previews
        WHERE session_key = ? AND session_id = ? ORDER BY updated_at DESC""",
        (session_key, session_id)).fetchall())
    return [{"turn_id": row[0], "content": row[1], "message_id": row[2]} for row in rows]


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
        if isinstance(content, str) and content.startswith(_SPLIT_PREVIEW_PREFIX):
            try:
                message_ids = json.loads(content[len(_SPLIT_PREVIEW_PREFIX):])
            except (TypeError, ValueError):
                message_ids = []
            await asyncio.to_thread(record_split_preview, turn_id=token, session_key=ctx.session_key,
                                    session_id=ctx.session_id, message_ids=message_ids)
            return
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
    preview = _load_preview_row(ctx.session_key, ctx.session_id, token)
    via_predecessor = False
    # If the process died after publishing the successor active-turn token but
    # before the preview re-key transaction, startup promotion records the
    # predecessor token and keeps that exact chained row reachable.
    if not preview:
        rows = _load_session_preview_rows(ctx.session_key, ctx.session_id)
        predecessor = getattr(entry, "resume_predecessor_turn_id", None)
        if len(rows) == 1 and predecessor and rows[0]["turn_id"] == predecessor:
            preview = rows[0]
            via_predecessor = True
    if not preview:
        return
    if isinstance(preview.get("content"), str) and preview["content"].startswith(_REPLACEMENT_PREVIEW_PREFIX):
        try:
            intent = json.loads(preview["content"][len(_REPLACEMENT_PREVIEW_PREFIX):])
        except (TypeError, ValueError):
            intent = {}
        stale_ids = list(intent.get("stale_ids") or [])
        if preview.get("message_id"):
            stale_ids.append(preview["message_id"])
        delete = getattr(consumer, "_delete_previews", None)
        if delete and stale_ids:
            try:
                _delete_recovered_previews(
                    ctx, delete, list(dict.fromkeys(stale_ids)), label="Recovered replacement preview",
                )
            except Exception:
                logger.warning("Could not clean recovered replacement preview for %s", ctx.session_key,
                               exc_info=True)
        discard_preview(preview["turn_id"])
        consumer._recovered_send_uncertain = True
        consumer.cfg.buffer_only = True
        return
    if isinstance(preview.get("content"), str) and preview["content"].startswith(_SPLIT_PREVIEW_PREFIX):
        try:
            head_ids = json.loads(preview["content"][len(_SPLIT_PREVIEW_PREFIX):])
        except (TypeError, ValueError):
            head_ids = []
        # A crash in the split-send window leaves sealed heads on screen. Remove
        # those owned messages before the resumed answer is allowed to send.
        delete = getattr(consumer, "_delete_previews", None)
        if delete and head_ids:
            try:
                _delete_recovered_previews(ctx, delete, head_ids, label="Recovered split heads")
            except Exception:
                logger.warning("Could not clean recovered split heads for %s", ctx.session_key, exc_info=True)
        # The found row may be the predecessor's, not this resume token's.
        discard_preview(preview["turn_id"])
        return
    # Re-own the row under this resumed turn's own active token: if it crashes too,
    # promotion makes that token the next resume_turn_id and recovery still finds it.
    successor = getattr(event, "_gateway_active_turn_token", None)
    predecessor = preview["turn_id"]
    if successor and successor != predecessor:
        try:
            # Re-key and retire the predecessor in one SQLite transaction. A
            # failed transaction leaves the predecessor reachable for the next
            # recovery attempt instead of publishing an unreachable successor.
            def _reown(conn):
                now = time.time()
                conn.execute("""INSERT INTO streamed_previews
                    (turn_id, session_key, session_id, content, message_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(turn_id) DO UPDATE SET session_key=excluded.session_key,
                        session_id=excluded.session_id, content=excluded.content,
                        message_id=excluded.message_id, updated_at=excluded.updated_at""",
                    (successor, ctx.session_key, ctx.session_id, preview["content"],
                     preview["message_id"], now))
                conn.execute("DELETE FROM streamed_previews WHERE turn_id = ?", (predecessor,))
            _journal(_reown, busy_timeout_ms=_REOWN_BUSY_TIMEOUT_MS)
        except Exception:
            logger.warning("Could not re-own streamed preview for %s", ctx.session_key, exc_info=True)
            if via_predecessor:
                # The row is keyed by the predecessor, which the next promotion would
                # forget: another crash could not find it again. Leave it in place and
                # treat this turn like an ACK-lost send rather than adopt an unreachable owner.
                consumer._recovered_send_uncertain = True
                consumer.cfg.buffer_only = True
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
