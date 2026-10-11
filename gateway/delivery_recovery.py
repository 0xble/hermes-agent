"""Cancellation settlement for runtime delivery-ledger claims."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

_T = TypeVar("_T")


class RuntimeDeliverySettlement:
    """Defer teardown cancellation until the same claim/send/refund operation settles.

    Adapted from NousResearch/hermes-agent#107135 without its older runner. A DB
    thread may commit after its awaiting task is cancelled: never restart that
    operation, or a repeated refund could alter a successor's newer claim.
    """

    def __init__(self) -> None:
        self.cancelled = False
        self.blocked_chats: dict = {}

    async def wait(self, operation: Awaitable[_T]) -> _T:
        task = asyncio.ensure_future(operation)
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                self.cancelled = True
