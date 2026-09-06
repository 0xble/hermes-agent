"""Bounded admission for the shared gateway executor, without timing out running work."""

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
import logging
import threading

logger = logging.getLogger(__name__)


class GatewayCapacityError(RuntimeError):
    def __init__(self):
        super().__init__(
            "Gateway is busy and its queue is full. This request was not started. Please try again shortly."
        )


class GatewayWorkFuture(Future):
    def __init__(self, *, queued: bool):
        super().__init__()
        self.queued = queued


class GatewayExecutor(ThreadPoolExecutor):
    """One waiting wave beyond the existing workers. Cancelled queue entries keep
    their admission until dequeued, so cancellation cannot grow the queue without bound.
    Running work retains admission even if its asyncio waiter is cancelled.
    """

    def __init__(self, max_workers=32, thread_name_prefix="hermes-gateway"):
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self._admission_lock = threading.Lock()
        self._outstanding = 0

    def submit(self, fn, /, *args, **kwargs):
        with self._admission_lock:
            if self._outstanding >= 2 * self._max_workers:
                logger.warning("Gateway executor admission rejected: capacity full")
                raise GatewayCapacityError()
            result = GatewayWorkFuture(queued=self._outstanding >= self._max_workers)
            self._outstanding += 1

        def run():
            if not result.set_running_or_notify_cancel():
                return
            try:
                value = fn(*args, **kwargs)
            except BaseException as exc:
                result.set_exception(exc)
            else:
                result.set_result(value)

        def released(work=None):
            with self._admission_lock:
                self._outstanding -= 1
            if work is not None and work.cancelled():
                result.cancel()

        try:
            work = super().submit(run)
        except BaseException:
            released()
            raise
        work.add_done_callback(released)
        if result.queued:
            logger.info("Gateway executor work queued: all worker slots admitted")
        return result


async def run_gateway_work(executor, func, args, on_queued=None, on_not_started=None):
    try:
        work = executor.submit(copy_context().run, func, *args)
    except BaseException:
        if on_not_started is not None:
            on_not_started()
        raise
    if on_not_started is not None:
        work.add_done_callback(lambda future: on_not_started() if future.cancelled() else None)
    result = asyncio.wrap_future(work)
    try:
        if getattr(work, "queued", False) and on_queued is not None:
            try:
                await on_queued()
            except Exception:
                logger.warning("Could not deliver gateway queue notice", exc_info=True)
        return await result
    except BaseException:
        # Notification cancellation must also cancel work that has not started.
        result.cancel()
        work.cancel()
        raise


def start_turn_work(runner, turn_ctx, worker, run_sync):
    admission_started = False

    async def submit():
        nonlocal admission_started
        admission_started = True
        return await run_turn_work(runner, turn_ctx, worker, run_sync)

    task = asyncio.ensure_future(submit())
    task.add_done_callback(
        lambda done: worker.worker_done.set() if done.cancelled() and not admission_started else None,
    )
    return task


async def run_turn_work(runner, turn_ctx, worker, run_sync):
    from gateway.run import _interim_metadata

    async def queued():
        adapter = runner._adapter_for_source(turn_ctx.source)
        if adapter:
            await adapter.send(
                turn_ctx.source.chat_id,
                "The gateway is busy. Your request is queued and will start when a worker is available.",
                metadata=_interim_metadata(turn_ctx._status_thread_metadata),
            )

    try:
        return await runner._run_in_executor_with_context(
            run_sync, _on_queued=queued, _on_not_started=worker.worker_done.set,
        )
    except GatewayCapacityError as exc:
        worker.worker_done.set()
        return {
            "final_response": str(exc), "messages": [], "failed": True,
            "api_calls": 0, "history_offset": 0, "response_previewed": False,
        }
    except RuntimeError:
        # Executor shutdown may reject before a future exists. A worker-raised
        # RuntimeError has already run its lifecycle finally block by this point.
        worker.worker_done.set()
        raise
