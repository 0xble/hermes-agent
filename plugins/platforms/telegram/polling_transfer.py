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
from dataclasses import dataclass, field
from contextlib import contextmanager

from telegram import Update

from gateway.deadline import deadline_scope, now as gateway_deadline_now, remaining as gateway_deadline_remaining
from gateway.deadline import detached_context
from gateway.generation import GenerationCoordinator

_RETENTION_SECONDS = 24 * 60 * 60
# Bound for one poller lifecycle evidence write (lock wait + commit).
POLLER_EVIDENCE_WRITE_SECONDS = 5
_IDLE_RESET_SECONDS = 7 * 24 * 60 * 60
_MAX_RAW_UPDATE = 1024 * 1024


@dataclass
class PollingJournal:
    coordinator: GenerationCoordinator
    token: str = field(repr=False)

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

    def lifecycle_owner(self) -> tuple[str, int] | None:
        """Bind polling evidence to this process's current generation lease."""
        import os
        with self._connect() as db:
            row = db.execute("SELECT g.id,l.epoch,g.pid,g.start_fingerprint,g.state,g.verdict FROM leases l JOIN generations g "
                             "ON g.id=l.generation_id WHERE l.resource='active_generation' "
                             "AND l.state='active'").fetchone()
            if row is None or row["pid"] != os.getpid():
                if db.execute("SELECT 1 FROM generations LIMIT 1").fetchone():
                    raise RuntimeError("polling generation identity is not serving")
                return None  # Standalone controlled polling has no generation identity.
        from gateway.status import _get_process_start_time
        started = _get_process_start_time(os.getpid())
        if (started is None or row["start_fingerprint"] != f"{os.getpid()}:{started}"
                or row["state"] != "serving" or row["verdict"] is not None):
            raise RuntimeError("polling generation identity is not serving")
        return row["id"], row["epoch"]

    def record_lifecycle(self, owner: tuple[str, int], event: str, *,
                         monotonic_at: float, wall_at: float) -> None:
        # Each evidence write gets its own named bound so a held coordinator lock
        # cannot stall it; an enclosing (earlier) deadline still wins.
        with deadline_scope(gateway_deadline_now() + POLLER_EVIDENCE_WRITE_SECONDS):
            self.coordinator.record_poller_event(self.token_hash, owner[0], owner[1], event,
                                                 monotonic_at=monotonic_at, wall_at=wall_at)

    def record_response(self, payload: bytes) -> None:
        envelope = json.loads(payload)
        if not isinstance(envelope, dict) or envelope.get("ok") is not True:
            return
        updates = envelope.get("result")
        if not isinstance(updates, list):
            raise ValueError("invalid getUpdates result")
        if not updates:
            with self._connect() as db:
                db.execute("DELETE FROM telegram_updates WHERE token_hash=? AND state IN ('accepted','quarantined','processing') AND received_at<?",
                           (self.token_hash, time.time() - _RETENTION_SECONDS))
            return
        rows = []
        for item in updates:
            if not isinstance(item, dict) or type(item.get("update_id")) is not int:
                logger.warning("Quarantining malformed Telegram update in polling journal")
                continue
            raw = json.dumps(item, separators=(",", ":")).encode()
            if len(raw) > _MAX_RAW_UPDATE:
                logger.warning("Quarantining oversized Telegram update %s", item["update_id"])
                rows.append((self.token_hash, item["update_id"], b"{}", "quarantined", time.time()))
                continue
            rows.append((self.token_hash, item["update_id"], raw, "received", time.time()))
        if not rows:
            return
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("SELECT confirmed_offset,updated_at FROM polling_cursors WHERE token_hash=?",
                                (self.token_hash,)).fetchone()
            # Telegram may assign random IDs after a week idle. The cursor's
            # update time also bounds all unadmitted rows from this token.
            idle = time.time() - cursor["updated_at"] >= _IDLE_RESET_SECONDS
            if idle:
                db.execute("DELETE FROM telegram_updates WHERE token_hash=?", (self.token_hash,))
                previous = 0
            else:
                previous = cursor["confirmed_offset"]
            db.executemany("INSERT OR IGNORE INTO telegram_updates VALUES (?,?,?,?,?)", rows)
            db.execute("UPDATE polling_cursors SET confirmed_offset=?,updated_at=? WHERE token_hash=?",
                       (max(previous, max(row[1] for row in rows) + 1), time.time(), self.token_hash))
            db.execute("DELETE FROM telegram_updates WHERE token_hash=? AND state IN ('accepted','quarantined','processing') AND received_at<?",
                       (self.token_hash, time.time() - _RETENTION_SECONDS))
            db.commit()

    def safe_offset(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT confirmed_offset,updated_at FROM polling_cursors WHERE token_hash=?",
                             (self.token_hash,)).fetchone()
            if not row:
                return 0
            if time.time() - row["updated_at"] >= _IDLE_RESET_SECONDS:
                return 0
            return row["confirmed_offset"]

    def quarantined(self, update_ids: list[int]) -> set[int]:
        if not update_ids:
            return set()
        with self._connect() as db:
            placeholders = ",".join("?" for _ in update_ids)
            rows = db.execute(
                f"SELECT update_id FROM telegram_updates WHERE token_hash=? AND state='quarantined' AND update_id IN ({placeholders})",
                (self.token_hash, *update_ids)).fetchall()
        return {row["update_id"] for row in rows}

    def pending(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT update_id,raw_update FROM telegram_updates WHERE token_hash=? AND state='received' ORDER BY update_id",
                              (self.token_hash,)).fetchall()
        pending = []
        for row in rows:
            try:
                decoded = json.loads(row["raw_update"])
                if not isinstance(decoded, dict) or decoded.get("update_id") != row["update_id"]:
                    raise ValueError("invalid replayed update identity")
                pending.append(decoded)
            except (ValueError, TypeError):
                logger.warning("Quarantining undecodable Telegram update %s", row["update_id"])
                self.quarantine(row["update_id"])
        return pending

    def quarantine(self, update_id: int) -> None:
        with self._connect() as db:
            db.execute("UPDATE telegram_updates SET state='quarantined' WHERE token_hash=? AND update_id=? AND state='received'",
                       (self.token_hash, update_id))

    async def claim(self, update_id: int) -> bool:
        def write() -> bool:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute("UPDATE telegram_updates SET state='processing' WHERE token_hash=? AND update_id=? AND state='received'",
                                     (self.token_hash, update_id)).rowcount
                db.commit()
                return bool(changed)
        return await asyncio.to_thread(write)

    async def accept(self, update_id: int) -> None:
        def write() -> None:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE telegram_updates SET state='accepted' WHERE token_hash=? AND update_id=? AND state='processing'",
                           (self.token_hash, update_id))
                db.commit()
        await asyncio.to_thread(write)

    async def reopen(self, update_id: int) -> None:
        def write() -> None:
            with self._connect() as db:
                db.execute("UPDATE telegram_updates SET state='received' WHERE token_hash=? AND update_id=? AND state='processing'",
                           (self.token_hash, update_id))
        await asyncio.to_thread(write)

    def stop_receipt(self) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT epoch FROM polling_cursors WHERE token_hash=?", (self.token_hash,)).fetchone()
        return {"token_hash": self.token_hash, "epoch": row["epoch"], "safe_offset": self.safe_offset()}

    def validate_transfer(self, receipt: dict) -> bool:
        if not isinstance(receipt, dict) or receipt.get("token_hash") != self.token_hash:
            return False
        safe_offset = receipt.get("safe_offset")
        if not isinstance(safe_offset, int) or isinstance(safe_offset, bool):
            raise RuntimeError("polling transfer receipt has an invalid safe_offset")
        current = self.stop_receipt()
        return receipt.get("epoch") == current["epoch"] and safe_offset <= current["safe_offset"]

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
_active_pollers: dict[str, asyncio.Task] = {}


def token_has_active_poller(token_hash: str) -> bool:
    return _active_pollers.get(token_hash) is not None


class ControlledPoller:
    """One serial request at a time; stopping never abandons an outstanding request."""

    def __init__(self, app, journal: PollingJournal, *, timeout: float = 20,
                 on_error=None, on_failure=None, on_progress=None, lifecycle_predecessor=None):
        self.app = app
        self.journal = journal
        self.timeout = timeout
        self.on_error = on_error
        self.on_failure = on_failure
        self.on_progress = on_progress
        self._lifecycle_predecessor = lifecycle_predecessor
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._lifecycle_owner: tuple[str, int] | None = None
        self._lifecycle_task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self):
        if self.running or token_has_active_poller(self.journal.token_hash):
            raise RuntimeError("poller already running for token")
        self._lifecycle_owner = await self._check_lifecycle_owner()
        self._stop.clear()
        # Rows in processing may have crossed a handler's external-effect boundary.
        # Only explicitly failed pre-handoff claims reopen; do not replay ambiguous crashes.
        for raw in await asyncio.to_thread(self.journal.pending):
            try:
                update = Update.de_json(raw, self.app.bot)
                if update is None or type(update.update_id) is not int:
                    raise ValueError("invalid replayed update")
            except Exception:
                logger.warning("Quarantining undecodable Telegram update %s", raw.get("update_id"), exc_info=True)
                await asyncio.to_thread(self.journal.quarantine, raw["update_id"])
                continue
            await self.app.update_queue.put(update)
        await self._join_queue("replayed update")
        # Queue drain can outlive this lease. Revalidate at the wire boundary.
        self._lifecycle_owner = await self._check_lifecycle_owner()
        if self._lifecycle_owner is not None:
            # Evidence I/O must not gate the live wire. Preserve occurrence times
            # and flush this task before stop evidence and token-lock release.
            self._lifecycle_task = asyncio.create_task(self._record_lifecycle(
                self._lifecycle_owner, "poller_started", time.monotonic(), time.time()), context=detached_context())
        self._task = asyncio.create_task(self._run(), name="telegram-controlled-poller", context=detached_context())
        _active_pollers[self.journal.token_hash] = self._task
        self._task.add_done_callback(self._observe_task)
        await asyncio.sleep(0)
        if self._task.done():
            await self._task

    async def _check_lifecycle_owner(self) -> tuple[str, int] | None:
        try:
            return await asyncio.to_thread(self.journal.lifecycle_owner)
        except Exception as exc:
            logger.exception("Controlled Telegram poller identity check failed before startup")
            if self.on_failure is not None:
                self.on_failure(exc)
            if self.on_error is not None:
                self.on_error(exc)
            raise

    async def _record_lifecycle(self, owner, event, monotonic_at, wall_at) -> None:
        try:
            if event == "poller_started" and self._lifecycle_predecessor is not None:
                await self._lifecycle_predecessor
            await asyncio.to_thread(self.journal.record_lifecycle, owner, event,
                                    monotonic_at=monotonic_at, wall_at=wall_at)
        except Exception:
            # Missing evidence remains a failed journal check, never a clean
            # interval. It must not strand an otherwise healthy polling owner.
            logger.exception("Telegram polling lifecycle evidence could not be recorded: %s", event)

    async def _join_queue(self, batch: str) -> None:
        """Backpressure until dispatch catches up; a slow handler is not a poll failure."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self.app.update_queue.join(), timeout=self.timeout)
                return
            except asyncio.TimeoutError:
                logger.warning("Telegram polling waiting for %s queue to drain", batch)

    async def _run(self):
        failures = 0
        while not self._stop.is_set():
            try:
                updates = await self.app.bot.get_updates(
                    offset=await asyncio.to_thread(self.journal.safe_offset), timeout=self.timeout,
                    allowed_updates=Update.ALL_TYPES)
                # The dedicated request hook must have committed every returned ID.
                # A fake or miswired request is a hard failure, never an unsafe offset.
                if updates:
                    safe_offset = await asyncio.to_thread(self.journal.safe_offset)
                    if any(update.update_id >= safe_offset for update in updates):
                        raise RuntimeError("getUpdates response escaped the wire journal")
                    quarantined = await asyncio.to_thread(self.journal.quarantined, [update.update_id for update in updates])
                    for update in updates:
                        if update.update_id not in quarantined:
                            await self.app.update_queue.put(update)
                    await self._join_queue("received update")
                failures = 0
                if self.on_progress is not None:
                    self.on_progress()
            except asyncio.CancelledError:
                # Cancellation does not prove the remote long poll ended. A caller
                # may cancel the owner, but it may not release its token lock.
                raise
            except Exception as exc:
                failures += 1
                if self.on_failure is not None:
                    self.on_failure(exc)
                if failures >= 10:
                    if self.on_error is not None:
                        self.on_error(exc)
                    raise
                logger.warning("Telegram controlled polling failed (%d/10): %s", failures, type(exc).__name__)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=min(2 ** (failures - 1), 30))
                except asyncio.TimeoutError:
                    pass

    def _forget_task(self, task: asyncio.Task) -> None:
        if _active_pollers.get(self.journal.token_hash) is task:
            _active_pollers.pop(self.journal.token_hash, None)

    def _observe_task(self, task: asyncio.Task) -> None:
        if _active_pollers.get(self.journal.token_hash) is task:
            # A disconnect may have registered a lock-release callback on this
            # same task. Keep the in-process fence until that callback runs.
            asyncio.get_running_loop().call_soon(self._forget_task, task)
        if not task.cancelled():
            error = task.exception()  # Consume even if disconnect never runs.
            if error is not None:
                logger.warning("Controlled Telegram poller failed: %s", type(error).__name__)

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            try:
                # A cancelled HTTP task can finish before the Bot API has closed its
                # long poll. Only a complete response (or a finished request error)
                # proves the old request cannot overlap the successor.
                await asyncio.wait_for(asyncio.shield(self._task), timeout=self.timeout + 1)
                await asyncio.wait_for(self.app.update_queue.join(), timeout=self.timeout + 1)
            except asyncio.TimeoutError:
                return {"stopped": False, "error": "PollDrainTimeout"}
            except asyncio.CancelledError:
                if not self._task.cancelled():
                    raise
                return {"stopped": False, "error": "CancelledError"}
            except Exception as exc:
                return {"stopped": False, "error": type(exc).__name__}
        if self._lifecycle_owner is not None:
            stopped_at, wall_at = time.monotonic(), time.time()
            if self._lifecycle_task is not None:
                await self._lifecycle_task
                self._lifecycle_task = None
            await self._record_lifecycle(self._lifecycle_owner, "poller_stopped", stopped_at, wall_at)
            self._lifecycle_owner = None
        return {"stopped": True}
