"""Bounded, priority-ordered admission for restart replay work.

The scheduler deliberately owns admission, not durable recovery.  A dispatch failure
completes its item with an exception and releases the slot; callers retain ownership
of the durable source and may recover it using their existing policy.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

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


@dataclass(frozen=True)
class ReplayHandle:
    item: ReplayItem

    @property
    def future(self) -> asyncio.Future:
        assert self.item.future is not None
        return self.item.future


class ReplayScheduler:
    """A small FIFO-within-priority heap and fixed number of async workers."""

    def __init__(self, concurrency: int = 2):
        try:
            concurrency = int(concurrency)
        except (TypeError, ValueError):
            concurrency = 2
        self.concurrency = max(1, concurrency)
        self._queue: list[ReplayItem] = []
        self._sequence = itertools.count()
        self._condition = asyncio.Condition()
        self._workers: set[asyncio.Task] = set()
        self._started = False
        self._active = 0
        self._closed = False

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
        )
        heapq.heappush(self._queue, item)
        if self._started:
            self._ensure_workers()
        depth = len(self._queue)
        logger.info(
            "Replay scheduler enqueue: priority=%d kind=%s session_key=%s queue_depth=%d",
            priority, kind, session_key or "", depth,
            extra={
                "replay_scheduler": "enqueue",
                "priority": priority,
                "kind": kind,
                "session_key": session_key,
                "queue_depth": depth,
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
        # synchronous and intentionally does not await.  The notification task is
        # tiny and is not a replay worker, so it cannot consume the cap.
        asyncio.get_running_loop().create_task(_notify())

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
                while not self._queue and not self._closed:
                    await self._condition.wait()
                if self._closed and not self._queue:
                    return
                item = heapq.heappop(self._queue)
                self._active += 1
                depth = len(self._queue)
            logger.info(
                "Replay scheduler start: priority=%d kind=%s session_key=%s queue_depth=%d",
                item.priority, item.kind, item.session_key or "", depth,
                extra={
                    "replay_scheduler": "start",
                    "priority": item.priority,
                    "kind": item.kind,
                    "session_key": item.session_key,
                    "queue_depth": depth,
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

    async def wait_for(self, handle: ReplayHandle) -> Any:
        """Wait for one admission/turn without changing its retry semantics."""
        return await handle.future

    async def close(self) -> None:
        """Stop idle workers after queued work has drained; never cancel active work."""
        self._closed = True
        async with self._condition:
            self._condition.notify_all()
        if self._workers:
            await asyncio.gather(*tuple(self._workers), return_exceptions=True)
