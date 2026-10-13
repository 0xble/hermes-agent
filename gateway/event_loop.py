"""Bounded process-entry cleanup before the gateway's hard-exit funnel."""
from __future__ import annotations

import asyncio
import logging
import weakref

logger = logging.getLogger(__name__)
_TASK_CLEANUP_TIMEOUT = 2.0
_ASYNCGEN_CLEANUP_TIMEOUT = 1.0
_teardown_writes = weakref.WeakSet()


def create_teardown_write(coro):
    """Keep an already-shielded durable write out of the global cancellation sweep.

    Its owner still receives cancellation and waits for the write. Both share
    the same bounded cleanup budget; a wedged write cannot delay hard exit.
    """
    task = asyncio.create_task(coro)
    _teardown_writes.add(task)
    return task


async def _bounded_cleanup(loop):
    pending = asyncio.all_tasks(loop) - {asyncio.current_task(loop)}
    for task in pending:
        if task not in _teardown_writes:
            task.cancel()
    if pending:
        done, unfinished = await asyncio.wait(pending, timeout=_TASK_CLEANUP_TIMEOUT)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                loop.call_exception_handler({
                    "message": "unhandled exception during gateway teardown",
                    "exception": task.exception(), "task": task,
                })
        if unfinished:
            logger.warning("Gateway teardown left %d tasks after bounded cleanup", len(unfinished))

    # wait_for can exceed its timeout while waiting for cancellation to finish.
    # wait has a hard deadline even if an async-generator finalizer ignores it.
    shutdown = asyncio.create_task(loop.shutdown_asyncgens())
    done, _ = await asyncio.wait({shutdown}, timeout=_ASYNCGEN_CLEANUP_TIMEOUT)
    if shutdown in done:
        await shutdown
    else:
        shutdown.cancel()
        logger.warning("Gateway async-generator shutdown exceeded its cleanup budget")


def run_until_teardown(coro):
    """Cancel tasks and finalize async generators, but never join executor workers.

    ``asyncio.run`` joins its executor before returning, so a stuck worker can
    prevent the gateway's existing ``os._exit`` backstop from ever running.
    Cleanup gets at most three seconds; callers must then hard-exit. The loop
    stays open so a still-running worker can finish without a closed-loop race.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(_bounded_cleanup(loop))
