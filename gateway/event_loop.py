"""Process-entry loop ownership for gateways that hard-exit after teardown."""
from __future__ import annotations

import asyncio


def run_until_teardown(coro):
    """Return to the hard-exit funnel without joining default-executor workers.

    ``asyncio.run`` joins its executor before returning, so a stuck worker can
    prevent the gateway's existing ``os._exit`` backstop from ever running.
    This loop is intentionally not finalized: callers must hard-exit after
    the gateway coroutine completes its own graceful shutdown.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)
