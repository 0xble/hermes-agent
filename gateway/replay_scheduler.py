"""Bounded, priority-ordered admission for restart replay and background turns.

The scheduler deliberately owns admission, not durable recovery.  A dispatch failure
completes its item with an exception and releases the slot; callers retain ownership
of the durable source and may recover it using their existing policy.

Gated items are turns nobody typed (restart resumes). They start only while the shared
``BackgroundTurnGate`` has room, so a flood of them cannot crowd typed input off the
shared state.db writer and loop.  Ungated items never wait on the gate.  Wakes that are
not queued here ask the same gate directly (``try_reserve``) and are refused when full.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

logger = logging.getLogger("gateway.run")

REPLAY_PRIORITY_STARTUP = 1
REPLAY_PRIORITY_RESUME = 2
REPLAY_PRIORITY_SYNTHETIC = 3


@dataclass(order=True)
class ReplayItem:
    priority: int
    sequence: int
    kind: str = field(compare=False)
    session_key: Optional[str] = field(compare=False, default=None)
    profile_home: Any = field(compare=False, default=None)
    dispatch: Optional[Callable[[], Awaitable[Any]]] = field(compare=False, default=None)
    future: Optional[asyncio.Future] = field(compare=False, default=None)
    on_drop: Optional[Callable[[], Any]] = field(compare=False, default=None)
    gated: bool = field(compare=False, default=False)
    enqueued_at: float = field(compare=False, default=0.0)


class BackgroundTurnGate:
    """Room for one more background turn while fewer than ``limit`` run.

    Load is measured, not counted: ``running_keys`` returns sessions in the gated lanes,
    so a turn that ends, crashes or is stopped frees its slot without any
    release bookkeeping. Each reservation is a token owned by one caller and counts its
    session until ``claimed_since`` reports a turn started there after the reservation (or
    ``reservation_s`` passes for an admission that never ran), so reservations made before
    their turn registers cannot overshoot the limit. Once ``closing`` (the gateway is
    draining), queued gated items are dropped, not started.

    Scheduler grants are spaced so a queued backlog never starts as one burst of turn setup
    writes; they are serialized here, so there is no herd for jitter to de-synchronize.
    ``try_reserve`` is not spaced: its callers are refused rather than queued and retry on
    their own cadence, so spacing them would only delay wakes while slots are free.
    """

    def __init__(
        self, limit: int, running_keys: Callable[[], Iterable[str]], *,
        claimed_since: Optional[Callable[[Any, float], bool]] = None,
        closing: Callable[[], bool] = lambda: False,
        spacing_s: float = 1.0, reservation_s: float = 30.0,
    ):
        self.limit = max(1, int(limit))
        self._running_keys = running_keys
        self._claimed_since = claimed_since
        self.closing = closing
        self._spacing_s = max(0.0, float(spacing_s))
        self._reservation_s = float(reservation_s)
        # token -> (session key, granted wall time, expiry)
        self._reserved: dict[object, tuple[Any, float, float]] = {}
        self._next_grant_at = 0.0

    def load(self) -> int:
        now = time.monotonic()
        claimed = self._claimed_since or (lambda _key, _since: False)
        self._reserved = {
            token: (key, granted, until) for token, (key, granted, until) in self._reserved.items()
            if until > now and not claimed(key, granted)
        }
        return len(set(self._running_keys()) | {key for key, _granted, _until in self._reserved.values()})

    def retry_after(self) -> Optional[float]:
        """None when a queued background turn may start now, else seconds until the next check."""
        wait = self._next_grant_at - time.monotonic()
        if wait > 0:
            return wait
        return None if self.load() < self.limit else 1.0

    def grant(self, item: "ReplayItem") -> None:
        self._reserve(item.session_key or ("item", item.sequence))
        self._next_grant_at = time.monotonic() + self._spacing_s

    def try_reserve(self, key: Any) -> Optional[object]:
        """A reservation token for ``key`` if a background turn may start now, else None."""
        return self._reserve(key) if self.load() < self.limit else None

    def release(self, token: object) -> None:
        """Return a reservation whose admission was refused before any turn claimed it."""
        self._reserved.pop(token, None)

    def _reserve(self, key: Any) -> object:
        token = object()
        self._reserved[token] = (key, time.time(), time.monotonic() + self._reservation_s)
        return token


@dataclass(frozen=True)
class ReplayHandle:
    item: ReplayItem

    @property
    def future(self) -> asyncio.Future:
        assert self.item.future is not None
        return self.item.future


class ReplayScheduler:
    """A small FIFO-within-priority heap and fixed number of async workers."""

    def __init__(self, concurrency: int = 2, *, gate: Optional[BackgroundTurnGate] = None):
        try:
            concurrency = int(concurrency)
        except (TypeError, ValueError):
            concurrency = 2
        self.concurrency = max(1, concurrency)
        self._queue: list[ReplayItem] = []
        self._sequence = itertools.count()
        self._condition = asyncio.Condition()
        self._workers: set[asyncio.Task] = set()
        self._wake_tasks: set[asyncio.Task] = set()
        self._started = False
        self._active = 0
        self._closed = False
        self.gate = gate

    def start(self) -> None:
        """Start workers after a caller has queued one synchronous admission batch."""
        if not self._started and not self._closed:
            self._started = True
            self._ensure_workers()

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def active_count(self) -> int:
        return self._active

    def enqueue(
        self,
        *,
        priority: int,
        kind: str,
        session_key: Optional[str],
        dispatch: Callable[[], Awaitable[Any]],
        profile_home: Any = None,
        on_drop: Optional[Callable[[], Any]] = None,
        gated: bool = False,
    ) -> ReplayHandle:
        if self._closed:
            raise RuntimeError("replay scheduler is closed")
        loop = asyncio.get_running_loop()
        item = ReplayItem(
            priority=priority,
            sequence=next(self._sequence),
            kind=kind,
            session_key=session_key,
            profile_home=profile_home,
            dispatch=dispatch,
            future=loop.create_future(),
            on_drop=on_drop,
            gated=gated and self.gate is not None,
            enqueued_at=time.monotonic(),
        )
        heapq.heappush(self._queue, item)
        if self._started:
            self._ensure_workers()
        depth = len(self._queue)
        logger.info(
            "Replay scheduler enqueue: priority=%d kind=%s session_key=%s queue_depth=%d gated=%s",
            priority, kind, session_key or "", depth, item.gated,
            extra={
                "replay_scheduler": "enqueue",
                "priority": priority,
                "kind": kind,
                "session_key": session_key,
                "queue_depth": depth,
                "gated": item.gated,
            },
        )
        # The scheduler is only called on the gateway loop.  Wake workers without
        # making the producer yield or allowing a lower-priority item to overtake
        # later items queued in the same synchronous admission batch.
        if self._started:
            self._wake_workers()
        return ReplayHandle(item)

    def _ensure_workers(self) -> None:
        while len(self._workers) < self.concurrency:
            worker = asyncio.get_running_loop().create_task(self._worker())
            self._workers.add(worker)
            worker.add_done_callback(self._worker_done)

    def _wake_workers(self) -> None:
        async def _notify() -> None:
            async with self._condition:
                self._condition.notify_all()

        # notify() must run under the condition lock, but enqueue itself is
        # synchronous and intentionally does not await.  The notification task
        # is tiny and is not a replay worker, so it cannot consume the cap.
        task = asyncio.get_running_loop().create_task(_notify())
        self._wake_tasks.add(task)
        task.add_done_callback(self._wake_tasks.discard)

    def _worker_done(self, worker: asyncio.Task) -> None:
        self._workers.discard(worker)
        if not worker.cancelled():
            exc = worker.exception()
            if exc is not None:
                logger.warning("Replay scheduler worker failed", exc_info=exc)
        if self._queue and not self._closed:
            self._ensure_workers()
            self._wake_workers()

    async def _worker(self) -> None:
        while True:
            async with self._condition:
                # A drained batch owns no idle workers. A later enqueue recreates the pool.
                if not self._queue:
                    return
                dropped = self._take_gated() if self.gate is not None and self.gate.closing() else []
                item, retry_after = (None, 0.0) if dropped else self._pop_ready()
                if item is None and not dropped:
                    # Only gated items remain and the gate is full: re-check as turns end.
                    try:
                        await asyncio.wait_for(self._condition.wait(), retry_after)
                    except TimeoutError:
                        pass
                    continue
                if item is not None:
                    self._active += 1
                depth = len(self._queue)
            if dropped:
                # A draining gateway starts no new background turn; queued resumes release their
                # preclaims so the drain does not wait on turns that were never going to run.
                await self._drop_items(dropped)
                continue
            waited = time.monotonic() - item.enqueued_at
            logger.info(
                "Replay scheduler start: priority=%d kind=%s session_key=%s queue_depth=%d waited=%.1fs",
                item.priority, item.kind, item.session_key or "", depth, waited,
                extra={
                    "replay_scheduler": "start",
                    "priority": item.priority,
                    "kind": item.kind,
                    "session_key": item.session_key,
                    "queue_depth": depth,
                    "waited_s": round(waited, 3),
                },
            )
            outcome = "ok"
            future = item.future
            dispatch = item.dispatch
            assert future is not None
            assert dispatch is not None
            try:
                result = await dispatch()
            except asyncio.CancelledError:
                outcome = "cancelled"
                if not future.done():
                    future.cancel()
                raise
            except Exception as exc:
                outcome = "error"
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(result)
            finally:
                self._active -= 1
                depth = len(self._queue)
                logger.info(
                    "Replay scheduler finish: priority=%d kind=%s session_key=%s queue_depth=%d outcome=%s",
                    item.priority, item.kind, item.session_key or "", depth, outcome,
                    extra={
                        "replay_scheduler": "finish",
                        "priority": item.priority,
                        "kind": item.kind,
                        "session_key": item.session_key,
                        "queue_depth": depth,
                        "outcome": outcome,
                    },
                )
                async with self._condition:
                    self._condition.notify_all()

    def _pop_ready(self) -> tuple[Optional[ReplayItem], float]:
        """Take the first item in priority order that may start now.

        An ungated item always may. Every gated item shares one gate, so once the gate refuses,
        only ungated items behind it are considered.
        """
        retry_after: Optional[float] = None
        for item in sorted(self._queue):
            if item.gated:
                if retry_after is not None:
                    continue
                retry_after = self.gate.retry_after()
                if retry_after is not None:
                    continue
                self.gate.grant(item)
            self._queue.remove(item)
            heapq.heapify(self._queue)
            return item, 0.0
        return None, max(0.05, retry_after or 1.0)

    def promote(self, session_key: str) -> bool:
        """Start a queued gated item for ``session_key`` without waiting for the gate.

        A person is now waiting on that session, so its background turn is no longer background.
        """
        promoted = False
        for item in self._queue:
            if item.gated and item.session_key == session_key:
                item.gated = False
                item.priority = min(item.priority, REPLAY_PRIORITY_STARTUP)
                promoted = True
        if promoted:
            heapq.heapify(self._queue)
            logger.info("Replay scheduler promote: session_key=%s", session_key,
                        extra={"replay_scheduler": "promote", "session_key": session_key})
            if self._started:
                self._wake_workers()
        return promoted

    async def wait_for(self, handle: ReplayHandle) -> Any:
        """Wait for one admission/turn without changing its retry semantics."""
        return await handle.future

    async def wait_idle(self) -> None:
        """Wait until all currently queued admissions and active dispatches have completed."""
        async with self._condition:
            await self._condition.wait_for(lambda: not self._queue and not self._active)

    def _take_gated(self) -> list[ReplayItem]:
        dropped = [item for item in self._queue if item.gated]
        if dropped:
            self._queue[:] = [item for item in self._queue if not item.gated]
            heapq.heapify(self._queue)
        return dropped

    async def _drop_items(self, items: list[ReplayItem]) -> None:
        for item in items:
            if item.future is not None and not item.future.done():
                item.future.cancel()
            if item.on_drop is not None:
                try:
                    result = item.on_drop()
                    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                        await result
                except Exception:
                    logger.warning("Replay scheduler dropped-item cleanup failed", exc_info=True)

    async def close(self, timeout: float = 1.0) -> None:
        """Drop queued work and bounded-wait active workers during shutdown."""
        self._closed = True
        async with self._condition:
            dropped = list(self._queue)
            self._queue.clear()
            self._condition.notify_all()
        await self._drop_items(dropped)
        workers = tuple(self._workers)
        if workers:
            _done, pending = await asyncio.wait(workers, timeout=max(0.0, float(timeout)))
            if pending:
                for worker in pending:
                    worker.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        if self._wake_tasks:
            await asyncio.gather(*tuple(self._wake_tasks), return_exceptions=True)
