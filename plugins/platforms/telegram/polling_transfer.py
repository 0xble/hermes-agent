"""Opt-in, token-scoped Telegram wire journal for controlled polling.

A successful getUpdates response is committed here before PTB decodes it or the
next request can confirm its offset. An in-progress native callback is intentionally
not replayed: a crash can leave its external effects ambiguous.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from contextlib import contextmanager

from telegram import Update

from gateway.generation import GenerationCoordinator

_RETENTION_SECONDS = 24 * 60 * 60
_IDLE_RESET_SECONDS = 7 * 24 * 60 * 60
_MAX_RAW_UPDATE = 1024 * 1024


@dataclass
class PollingJournal:
    coordinator: GenerationCoordinator
    token: str

    def __post_init__(self):
        self.token_hash = hashlib.sha256(self.token.encode()).hexdigest()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS telegram_updates (
                    token_hash TEXT NOT NULL, update_id INTEGER NOT NULL,
                    raw_update BLOB NOT NULL, state TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    PRIMARY KEY(token_hash, update_id));
                CREATE TABLE IF NOT EXISTS polling_cursors (
                    token_hash TEXT PRIMARY KEY, confirmed_offset INTEGER NOT NULL,
                    epoch INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO polling_cursors VALUES (?,0,1,?)", (self.token_hash, time.time()))

    @contextmanager
    def _connect(self):
        db = self.coordinator.connect()
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def record_response(self, payload: bytes) -> None:
        envelope = json.loads(payload)
        if not isinstance(envelope, dict) or envelope.get("ok") is not True:
            return
        updates = envelope.get("result")
        if not isinstance(updates, list):
            raise ValueError("invalid getUpdates result")
        if not updates:
            with self._connect() as db:
                db.execute("DELETE FROM telegram_updates WHERE token_hash=? AND state='accepted' AND received_at<?",
                           (self.token_hash, time.time() - _RETENTION_SECONDS))
            return
        rows = []
        for item in updates:
            if not isinstance(item, dict) or type(item.get("update_id")) is not int:
                raise ValueError("invalid Telegram update ID")
            raw = json.dumps(item, separators=(",", ":")).encode()
            if len(raw) > _MAX_RAW_UPDATE:
                raise ValueError("Telegram update exceeds journal envelope limit")
            rows.append((self.token_hash, item["update_id"], raw, "received", time.time()))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("SELECT confirmed_offset,updated_at FROM polling_cursors WHERE token_hash=?",
                                (self.token_hash,)).fetchone()
            # Telegram may assign random IDs after a week idle. A fresh epoch then
            # starts at zero; never reset while unadmitted rows still need replay.
            idle = time.time() - cursor["updated_at"] >= _IDLE_RESET_SECONDS
            pending = db.execute("SELECT 1 FROM telegram_updates WHERE token_hash=? AND state!='accepted' LIMIT 1",
                                 (self.token_hash,)).fetchone()
            if idle and not pending:
                db.execute("DELETE FROM telegram_updates WHERE token_hash=?", (self.token_hash,))
                previous = 0
            else:
                previous = cursor["confirmed_offset"]
            db.executemany("INSERT OR IGNORE INTO telegram_updates VALUES (?,?,?,?,?)", rows)
            db.execute("UPDATE polling_cursors SET confirmed_offset=?,updated_at=? WHERE token_hash=?",
                       (max(previous, max(row[1] for row in rows) + 1), time.time(), self.token_hash))
            db.execute("DELETE FROM telegram_updates WHERE token_hash=? AND state='accepted' AND received_at<?",
                       (self.token_hash, time.time() - _RETENTION_SECONDS))
            db.commit()

    def safe_offset(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT confirmed_offset,updated_at FROM polling_cursors WHERE token_hash=?",
                             (self.token_hash,)).fetchone()
            if not row:
                return 0
            if time.time() - row["updated_at"] >= _IDLE_RESET_SECONDS:
                pending = db.execute("SELECT 1 FROM telegram_updates WHERE token_hash=? AND state!='accepted' LIMIT 1",
                                     (self.token_hash,)).fetchone()
                if not pending:
                    return 0
            return row["confirmed_offset"]

    def pending(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT raw_update FROM telegram_updates WHERE token_hash=? AND state='received' ORDER BY update_id",
                              (self.token_hash,)).fetchall()
        return [json.loads(row["raw_update"]) for row in rows]

    def claim(self, update_id: int) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE telegram_updates SET state='processing' WHERE token_hash=? AND update_id=? AND state='received'",
                                 (self.token_hash, update_id)).rowcount
            db.commit()
            return bool(changed)

    def accept(self, update_id: int) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE telegram_updates SET state='accepted' WHERE token_hash=? AND update_id=? AND state='processing'",
                       (self.token_hash, update_id))
            db.commit()

    def stop_receipt(self) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT epoch FROM polling_cursors WHERE token_hash=?", (self.token_hash,)).fetchone()
        return {"token_hash": self.token_hash, "epoch": row["epoch"], "safe_offset": self.safe_offset()}

    def validate_transfer(self, receipt: dict) -> bool:
        if not isinstance(receipt, dict) or receipt.get("token_hash") != self.token_hash:
            return False
        current = self.stop_receipt()
        return receipt.get("epoch") == current["epoch"] and receipt.get("safe_offset") <= current["safe_offset"]

    def begin_successor(self, receipt: dict) -> int:
        if not self.validate_transfer(receipt):
            raise RuntimeError("polling transfer receipt does not match durable journal")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE polling_cursors SET epoch=epoch+1 WHERE token_hash=? AND epoch=?",
                                 (self.token_hash, receipt["epoch"])).rowcount
            if not changed:
                raise RuntimeError("polling epoch already transferred")
            db.commit()
        return self.stop_receipt()["epoch"]


logger = logging.getLogger(__name__)


class ControlledPoller:
    """One serial request at a time; stopping never abandons an outstanding request."""

    def __init__(self, app, journal: PollingJournal, *, timeout: float = 20,
                 on_error=None, on_progress=None):
        self.app = app
        self.journal = journal
        self.timeout = timeout
        self.on_error = on_error
        self.on_progress = on_progress
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self):
        if self.running:
            raise RuntimeError("poller already running")
        self._stop.clear()
        # Replay before the very first offset that can acknowledge these rows.
        for raw in self.journal.pending():
            await self.app.update_queue.put(Update.de_json(raw, self.app.bot))
        await self.app.update_queue.join()
        self._task = asyncio.create_task(self._run(), name="telegram-controlled-poller")
        await asyncio.sleep(0)
        if self._task.done():
            await self._task

    async def _run(self):
        failures = 0
        while not self._stop.is_set():
            try:
                updates = await self.app.bot.get_updates(
                    offset=self.journal.safe_offset(), timeout=self.timeout,
                    allowed_updates=Update.ALL_TYPES)
                # The dedicated request hook must have committed every returned ID.
                # A fake or miswired request is a hard failure, never an unsafe offset.
                if updates:
                    safe_offset = self.journal.safe_offset()
                    if any(update.update_id >= safe_offset for update in updates):
                        raise RuntimeError("getUpdates response escaped the wire journal")
                    for update in updates:
                        await self.app.update_queue.put(update)
                    await self.app.update_queue.join()
                failures = 0
                if self.on_progress is not None:
                    self.on_progress()
            except asyncio.CancelledError:
                # Cancellation does not prove the remote long poll ended. A caller
                # may cancel the owner, but it may not release its token lock.
                raise
            except Exception as exc:
                failures += 1
                if failures >= 10:
                    if self.on_error is not None:
                        self.on_error(exc)
                    raise
                logger.warning("Telegram controlled polling failed (%d/10): %s", failures, type(exc).__name__)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=min(2 ** (failures - 1), 30))
                except asyncio.TimeoutError:
                    pass

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            # No deadline: a timeout/cancel is NOT a release receipt.
            await asyncio.shield(self._task)
