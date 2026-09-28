"""Per-profile admission and user-visible egress receipts for the opt-in gateway outbox.

The store does not retry an interrupted model turn. A dispatch that crossed the
transport boundary without a receipt is quarantined, never treated as unsent.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Context is copied into child tasks; only the task explicitly bound to ingress
# may use the turn's outbox. A separate background notification is not its egress.
_CURRENT_TURN: contextvars.ContextVar[tuple[Path, str, asyncio.Task | None] | None] = contextvars.ContextVar(
    "gateway_outbox_turn", default=None)
_BYPASS: contextvars.ContextVar[bool] = contextvars.ContextVar("gateway_outbox_transport", default=False)
_TURN_LOCKS: weakref.WeakValueDictionary[tuple[Path, str], asyncio.Lock] = weakref.WeakValueDictionary()
_STORES: dict[Path, "Outbox"] = {}
_RETRY_TASKS: dict[tuple[Path, str], asyncio.Task] = {}


def store_for(home: Path) -> "Outbox":
    key = Path(home).resolve()
    if key not in _STORES:
        _STORES[key] = Outbox(key)
    return _STORES[key]


def bind_turn(home: Path, turn_id: str) -> None:
    _CURRENT_TURN.set((Path(home), turn_id, asyncio.current_task()))


def clear_turn() -> None:
    _CURRENT_TURN.set(None)


def scoped_turn_entry():
    """Hide an inherited turn while processing another ingress; restore on return."""
    return _CURRENT_TURN.set(None)


def restore_turn(token: contextvars.Token) -> None:
    _CURRENT_TURN.reset(token)


async def run_turn_child(coro, turn: tuple[Path, str] | None):
    """Bind a turn-owned streaming worker, unlike unrelated background tasks."""
    if turn is None:
        return await coro
    token = scoped_turn_entry()
    try:
        bind_turn(*turn)
        return await coro
    finally:
        restore_turn(token)


@contextmanager
def transport_bypass():
    token = _BYPASS.set(True)
    try:
        yield
    finally:
        _BYPASS.reset(token)


def active_turn():
    turn = _CURRENT_TURN.get()
    if _BYPASS.get() or turn is None or turn[2] is not asyncio.current_task():
        return None
    return turn[:2]


def transport_id(event) -> str | None:
    update_id = getattr(event, "platform_update_id", None)
    if update_id is not None:
        return f"update:{update_id}"
    message_id = getattr(event, "message_id", None)
    if message_id is not None:
        return f"chat:{event.source.chat_id}:message:{message_id}"
    return getattr(event, "_outbox_transport_id", None)


def event_kind(event) -> str:
    return getattr(getattr(event, "message_type", None), "value", None) or "message"


def durable_control(method):
    """Control cards return a raw Telegram message so callback state can bind its ID."""
    @functools.wraps(method)
    async def wrapped(self, chat_id, text, *, parse_mode, thread_id, metadata,
                      reply_markup=None, reply_to_mode=None):
        config = getattr(getattr(self, "gateway_runner", None), "config", None)
        if not getattr(config, "durable_outbox_enabled", False) or active_turn() is None:
            return await method(self, chat_id, text, parse_mode=parse_mode,
                                thread_id=thread_id, metadata=metadata,
                                reply_markup=reply_markup, reply_to_mode=reply_to_mode)
        from gateway.platforms.base import SendResult
        payload = {
            "chat_id": chat_id, "text": text, "parse_mode": getattr(parse_mode, "value", parse_mode),
            "thread_id": thread_id, "metadata": metadata,
            "reply_markup": reply_markup.to_dict() if reply_markup is not None else None,
            "reply_to_mode": reply_to_mode,
        }
        message = None

        async def send(_payload):
            nonlocal message
            message = await method(self, chat_id, text, parse_mode=parse_mode,
                                   thread_id=thread_id, metadata=metadata,
                                   reply_markup=reply_markup, reply_to_mode=reply_to_mode)
            return SendResult(success=True, message_id=str(message.message_id))

        result = await deliver(self, "control_prompt", payload, send)
        if not result.success:
            raise RuntimeError(result.error or "control prompt outbox held")
        return message

    return wrapped


def durable_egress(kind: str):
    """Intercept an adapter's own egress only when opted in and inside a turn."""
    def decorate(method):
        signature = inspect.signature(method)

        @functools.wraps(method)
        async def wrapped(self, *args, **kwargs):
            config = getattr(getattr(self, "gateway_runner", None), "config", None)
            if not getattr(config, "durable_outbox_enabled", False) or active_turn() is None:
                return await method(self, *args, **kwargs)
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            payload = {k: v for k, v in bound.arguments.items() if k != "self" and k != "kwargs"}
            payload.update(bound.arguments.get("kwargs", {}))
            return await deliver(self, kind, payload, lambda p: method(self, **p))

        return wrapped
    return decorate


def _snapshot_file(home: Path, path: str) -> str:
    source = Path(path)
    target_dir = home / "gateway-outbox-media"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}-{source.name}"
    with source.open("rb") as inp, target.open("xb") as out:
        shutil.copyfileobj(inp, out)
        out.flush()
        os.fsync(out.fileno())
    descriptor = os.open(target_dir, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return str(target)


def wire_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "_outbox_original"}


def _discard_delivered_media(home: Path, payload: dict[str, Any]) -> None:
    """Only delete copies owned by this outbox after the receipt commits."""
    if "_outbox_original" not in payload:
        return
    root = (home / "gateway-outbox-media").resolve()
    paths = [payload[k] for k in ("file_path", "image_path", "video_path", "audio_path") if k in payload]
    from urllib.parse import unquote
    paths.extend(unquote(url[7:]) for url, _ in payload.get("images", []) if url.startswith("file://"))
    for path in paths:
        candidate = Path(path).resolve()
        if candidate.parent == root:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                logger.warning("Unable to remove delivered outbox media %s", candidate, exc_info=True)


def _uncertain(result) -> bool:
    """Only a transport outcome without a definitive refusal can have been sent."""
    if result.success or result.retry_after is not None or result.raw_response is not None:
        return False
    error = (result.error or "").lower()
    return (any(marker in error for marker in
                ("timeout", "timed out", "network error", "connection reset",
                 "connection aborted", "server disconnected")) or
            (result.retryable and not any(marker in error for marker in
             ("not connected", "bad request", "too_long", "draft_rejected", "flood_control"))))


async def deliver(adapter, kind: str, payload: dict[str, Any], send):
    """Commit an ordered row before crossing the transport boundary.

    ``send`` is the adapter's original send/edit/media coroutine. Any uncertain
    transport failure stays held; callers cannot silently turn it into a resend.
    """
    from gateway.platforms.base import SendResult

    turn = active_turn()
    if turn is None:
        return await send(payload)
    home, turn_id = turn
    store = store_for(home)
    lock = _TURN_LOCKS.setdefault(turn, asyncio.Lock())
    async with lock:
        # A repeated ambiguous payload cannot be safely retried, but unrelated
        # output in the same turn must not be starved by that uncertainty.
        if store.held_payload(turn_id, kind, payload):
            return SendResult(success=False, error="outbox payload has an uncertain prior dispatch")
        # Preserve the original request for certain non-delivery retries: a
        # scratch file's copied path differs from its original path.
        previous = store.pending_retry(turn_id, kind, payload)
        if previous is not None:
            row = previous
        else:
            durable = dict(payload)
            original_media = False
            for key in ("file_path", "image_path", "video_path", "audio_path"):
                if key in durable:
                    durable[key] = _snapshot_file(home, durable[key])
                    original_media = True
            if "images" in durable:
                from urllib.parse import quote, unquote
                images = []
                for url, alt in durable["images"]:
                    if url.startswith("file://"):
                        url = "file://" + quote(_snapshot_file(home, unquote(url[7:])))
                        original_media = True
                    images.append((url, alt))
                durable["images"] = images
            if original_media:
                durable["_outbox_original"] = payload
            try:
                row = store.enqueue(turn_id, kind, durable)
            except (TypeError, ValueError) as exc:
                logger.warning("Outbox could not serialize %s; using ordinary transport: %s", kind, exc)
                return await send(payload)
        if not store.begin_send(row):
            return SendResult(success=False, error="earlier outbox row is unresolved")
        try:
            with transport_bypass():
                result = await send(wire_payload(row.payload))
        except BaseException:
            store.receipt(row, message_id=None, success=False)
            raise
        retry_after = (result.retry_after if kind == "send" and
                       not (isinstance(payload.get("metadata"), dict) and
                            payload["metadata"].get("_interim_send")) else None)
        scheduled = store.receipt(row, message_id=str(result.message_id) if result.message_id else None,
                                  success=bool(result.success), uncertain=_uncertain(result),
                                  retry_after=retry_after)
        if scheduled:
            _schedule_retry(store, adapter)
        if result.success:
            _discard_delivered_media(home, row.payload)
        return result


def _schedule_retry(store: "Outbox", adapter) -> None:
    """One durable server-directed retry per row, without blocking ingress."""
    for row, deadline in store.scheduled():
        key = (store.path, row.idempotency_key)
        if key in _RETRY_TASKS:
            continue
        async def redeliver(key=key, deadline=deadline):
            try:
                await asyncio.sleep(max(0, deadline - time.time()))
                await recover(store, adapter)
            finally:
                _RETRY_TASKS.pop(key, None)
        task = asyncio.create_task(redeliver())
        _RETRY_TASKS[key] = task


async def recover(store: "Outbox", adapter) -> tuple[int, int]:
    """Replay only proven-unsent rows. 'sending' is always held for inspection."""
    sent = 0
    while True:
        pending = store.pending()
        if not pending:
            break
        advanced = False
        for row in pending:
            if row.type not in {"send", "edit_message", "send_document", "send_image_file",
                                "send_video", "send_voice", "send_multiple_images", "send_image",
                                "send_animation", "control_prompt"}:
                logger.error("Unsupported pending outbox type %s for %s", row.type, row.idempotency_key)
                continue
            if not store.begin_send(row):
                continue
            try:
                with transport_bypass():
                    if row.type == "control_prompt":
                        from gateway.platforms.base import SendResult
                        from telegram import InlineKeyboardMarkup
                        payload = wire_payload(row.payload)
                        markup = payload.get("reply_markup")
                        if markup is not None:
                            payload["reply_markup"] = InlineKeyboardMarkup.de_json(markup, adapter._bot)
                        message = await adapter._send_control_message(**payload)
                        result = SendResult(success=True, message_id=str(message.message_id))
                    else:
                        payload = wire_payload(row.payload)
                        if row.type == "send_multiple_images" and "images" in payload:
                            payload["images"] = [tuple(image) for image in payload["images"]]
                        result = await getattr(adapter, row.type)(**payload)
            except BaseException:
                store.receipt(row, message_id=None, success=False)
                logger.exception("Ambiguous outbox dispatch %s", row.idempotency_key)
                continue
            retry_after = (result.retry_after if row.type == "send" and
                           not (isinstance(row.payload.get("metadata"), dict) and
                                row.payload["metadata"].get("_interim_send")) else None)
            scheduled = store.receipt(row, message_id=str(result.message_id) if result.message_id else None,
                                      success=bool(result.success), uncertain=_uncertain(result),
                                      retry_after=retry_after)
            if scheduled:
                _schedule_retry(store, adapter)
            if result.success:
                _discard_delivered_media(store.path.parent, row.payload)
                sent += 1
                advanced = True
        if not advanced:
            break
    ambiguous = store.ambiguous()
    for row in ambiguous:
        logger.error("Held ambiguous outbox dispatch: turn=%s sequence=%s key=%s",
                     row.turn_id, row.sequence, row.idempotency_key)
    _schedule_retry(store, adapter)
    return sent, len(ambiguous)


@dataclass(frozen=True)
class OutboxRow:
    turn_id: str
    sequence: int
    type: str
    payload: dict[str, Any]
    idempotency_key: str
    owner_epoch: int
    state: str
    message_id: str | None


class Outbox:
    def __init__(self, home: Path):
        self.path = Path(home) / "gateway-outbox.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS admissions (
                    profile TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    transport_event_id TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    result TEXT,
                    PRIMARY KEY (profile, platform, transport_event_id, event_kind)
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    turn_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    owner_epoch INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending', 'sending', 'ambiguous', 'delivered', 'failed_unsent', 'expired_ambiguous')),
                    message_id TEXT,
                    send_status TEXT,
                    edit_status TEXT,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    retry_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (turn_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS outbox_state ON outbox(state, turn_id, sequence);
            """)
            schema = db.execute("SELECT sql FROM sqlite_master WHERE name='outbox'").fetchone()[0]
            if "failed_unsent" not in schema:
                db.executescript("""
                    DROP INDEX IF EXISTS outbox_state;
                    ALTER TABLE outbox RENAME TO outbox_old;
                    CREATE TABLE outbox (
                        turn_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                        type TEXT NOT NULL, payload TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE, owner_epoch INTEGER NOT NULL,
                        state TEXT NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending','sending','ambiguous','delivered','failed_unsent','expired_ambiguous')),
                        message_id TEXT, send_status TEXT, edit_status TEXT,
                        created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                        retry_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (turn_id, sequence)
                    );
                    INSERT INTO outbox (turn_id, sequence, type, payload, idempotency_key,
                                        owner_epoch, state, message_id, send_status, edit_status)
                        SELECT turn_id, sequence, type, payload, idempotency_key,
                               owner_epoch, state, message_id, send_status, edit_status FROM outbox_old;
                    DROP TABLE outbox_old;
                    CREATE INDEX outbox_state ON outbox(state, turn_id, sequence);
                """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = sqlite3.Row
        return db

    def lookup(self, profile: str, platform: str, transport_event_id: str,
               event_kind: str) -> tuple[str, str | None] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT turn_id, result FROM admissions WHERE profile=? AND platform=? "
                "AND transport_event_id=? AND event_kind=?",
                (profile, platform, transport_event_id, event_kind),
            ).fetchone()
            return (row[0], row[1]) if row else None

    def admit(self, profile: str, platform: str, transport_event_id: str | None,
              event_kind: str, *, fallback_id: str | None = None) -> tuple[str, bool]:
        """Return (original turn ID, newly admitted). Synthetic IDs must be durable upstream."""
        event_id = transport_event_id or fallback_id or uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                turn_id = uuid.uuid4().hex
                inserted = db.execute(
                    "INSERT OR IGNORE INTO admissions VALUES (?, ?, ?, ?, ?, NULL)",
                    (profile, platform, str(event_id), event_kind, turn_id),
                ).rowcount
                if not inserted:
                    turn_id = db.execute(
                        "SELECT turn_id FROM admissions WHERE profile=? AND platform=? "
                        "AND transport_event_id=? AND event_kind=?",
                        (profile, platform, str(event_id), event_kind),
                    ).fetchone()[0]
                db.commit()
                return turn_id, bool(inserted)
            except BaseException:
                db.rollback()
                raise

    def finish_admission(self, turn_id: str, result: str | None) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("UPDATE admissions SET result=? WHERE turn_id=?", (result, turn_id))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def original_result(self, turn_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT result FROM admissions WHERE turn_id=?", (turn_id,)).fetchone()
            return row[0] if row else None

    def pending_retry(self, turn_id: str, kind: str, payload: dict[str, Any]) -> OutboxRow | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM outbox WHERE turn_id=? ORDER BY sequence DESC LIMIT 1",
                             (turn_id,)).fetchone()
            if row and row["state"] == "pending" and row["retry_at"] is None and row["type"] == kind:
                stored = json.loads(row["payload"])
                if stored.get("_outbox_original", stored) == payload:
                    return self._row(row)
            return None

    def held_payload(self, turn_id: str, kind: str, payload: dict[str, Any]) -> bool:
        with self._connect() as db:
            for row in db.execute("SELECT payload FROM outbox WHERE turn_id=? AND type=? "
                                  "AND (state IN ('sending','ambiguous','expired_ambiguous') "
                                  "OR (state='pending' AND retry_at IS NOT NULL))", (turn_id, kind)):
                stored = json.loads(row[0])
                if stored.get("_outbox_original", stored) == payload:
                    return True
        return False

    def enqueue(self, turn_id: str, kind: str, payload: dict[str, Any], owner_epoch: int = 0,
                idempotency_key: str | None = None) -> OutboxRow:
        key = idempotency_key or uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute("SELECT * FROM outbox WHERE idempotency_key=?", (key,)).fetchone()
                if existing:
                    db.commit()
                    return self._row(existing)
                seq = db.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM outbox WHERE turn_id=?",
                                 (turn_id,)).fetchone()[0]
                db.execute("INSERT INTO outbox (turn_id, sequence, type, payload, idempotency_key, owner_epoch) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (turn_id, seq, kind, json.dumps(payload, default=str), key, owner_epoch))
                db.commit()
                return OutboxRow(turn_id, seq, kind, payload, key, owner_epoch, "pending", None)
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _row(row: sqlite3.Row) -> OutboxRow:
        return OutboxRow(row["turn_id"], row["sequence"], row["type"], json.loads(row["payload"]),
                         row["idempotency_key"], row["owner_epoch"], row["state"], row["message_id"])

    def pending(self) -> list[OutboxRow]:
        with self._connect() as db:
            return [self._row(r) for r in db.execute(
                "SELECT * FROM outbox WHERE state='pending' AND (retry_at IS NULL OR retry_at<=?) "
                "ORDER BY rowid", (time.time(),))]

    def scheduled(self) -> list[tuple[OutboxRow, float]]:
        with self._connect() as db:
            return [(self._row(r), r["retry_at"]) for r in db.execute(
                "SELECT * FROM outbox WHERE state='pending' AND retry_at>? ORDER BY retry_at",
                (time.time(),))]

    def all_rows(self) -> list[OutboxRow]:
        with self._connect() as db:
            return [self._row(r) for r in db.execute("SELECT * FROM outbox ORDER BY rowid")]

    def ambiguous(self) -> list[OutboxRow]:
        with self._connect() as db:
            expired = db.execute("UPDATE outbox SET state='expired_ambiguous' "
                                 "WHERE state IN ('sending','ambiguous') AND created_at<?",
                                 (time.time() - 86400,)).rowcount
            if expired:
                logger.error("Expired %s uncertain outbox sends without replay; inspect %s",
                             expired, self.path)
            return [self._row(r) for r in db.execute(
                "SELECT * FROM outbox WHERE state IN ('sending','ambiguous') ORDER BY rowid")]

    def status(self) -> list[dict[str, Any]]:
        self.ambiguous()  # expire old holds and log them
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT turn_id, sequence, type, idempotency_key, state, created_at, "
                "retry_at, attempts, send_status, edit_status FROM outbox "
                "WHERE state IN ('sending','ambiguous','expired_ambiguous','pending','failed_unsent') "
                "ORDER BY created_at DESC")]

    def begin_send(self, row: OutboxRow) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = db.execute("UPDATE outbox SET state='sending', retry_at=NULL, attempts=attempts+1 "
                                     "WHERE turn_id=? AND sequence=? AND state='pending' "
                                     "AND (retry_at IS NULL OR retry_at<=?)",
                                     (row.turn_id, row.sequence, time.time())).rowcount
                db.commit()
                return bool(changed)
            except BaseException:
                db.rollback()
                raise

    def receipt(self, row: OutboxRow, *, message_id: str | None, success: bool,
                uncertain: bool = True, retry_after: float | None = None) -> bool:
        """Return whether one server-directed, proven-unsent retry was queued."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                attempts = db.execute("SELECT attempts FROM outbox WHERE turn_id=? AND sequence=?",
                                      (row.turn_id, row.sequence)).fetchone()[0]
                scheduled = bool(not success and retry_after is not None and attempts == 1)
                state = ("delivered" if success else "pending" if scheduled else
                         "ambiguous" if uncertain else "failed_unsent")
                column = "edit_status" if row.type == "edit_message" else "send_status"
                db.execute(f"UPDATE outbox SET state=?, message_id=?, {column}=?, retry_at=? "
                           "WHERE turn_id=? AND sequence=? AND state='sending'",
                           (state, message_id, "success" if success else state,
                            time.time() + min(max(0, retry_after or 0), 86400) if scheduled else None,
                            row.turn_id, row.sequence))
                db.commit()
                return scheduled
            except BaseException:
                db.rollback()
                raise


if __name__ == "__main__":
    import argparse
    from hermes_constants import get_hermes_home

    parser = argparse.ArgumentParser(description="Inspect durable outbox unresolved deliveries")
    parser.add_argument("--home", type=Path, default=get_hermes_home())
    args = parser.parse_args()
    print(json.dumps(Outbox(args.home).status(), indent=2))
