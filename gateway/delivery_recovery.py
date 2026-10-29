"""Cancellation settlement for runtime delivery-ledger claims."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Optional, TypeVar

from gateway.config import Platform
from gateway.run_shutdown import _log_suppressed

logger = logging.getLogger("gateway.run")

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


class RuntimeDeliveryRecoveryMixin:
    """Runtime reconnect sweep and settlement methods for GatewayRunner."""

    async def _redeliver_failed_obligations_for_platform(
        self, platform: Platform, *, profile: Optional[str] = None,
    ) -> int:
        """Settle each runtime claim before honoring adapter teardown cancellation.

        The claim/refund worker can commit after its caller is cancelled. Shield
        the same operation until it completes, refund only undispatched rows,
        and settle an in-flight send rather than treating it as safely unsent.
        """
        settlement = RuntimeDeliverySettlement()
        try:
            from gateway.delivery_ledger import ledger_enabled, sweep_failed_for_runtime
            if not await asyncio.to_thread(ledger_enabled):
                return 0
            claimed = await settlement.wait(asyncio.to_thread(
                sweep_failed_for_runtime, platform.value, profile=profile,
            ))
        except Exception:
            logger.debug(
                "runtime delivery ledger sweep failed after %s reconnect", platform.value, exc_info=True,
            )
            if settlement.cancelled:
                raise asyncio.CancelledError()
            return 0
        sendable = []
        if claimed and not settlement.cancelled:
            # Clear before dispatch so recovery cannot redeliver AND resume a turn.
            sendable = await settlement.wait(
                self._clear_resume_pending_for_claimed_obligations(claimed, require_success=True)
            )
        sendable_ids = {row["obligation_id"] for row in sendable}
        redelivered = 0
        for row in claimed or []:
            if settlement.cancelled or row["obligation_id"] not in sendable_ids:
                await settlement.wait(self._release_runtime_claim_quiet(
                    row["obligation_id"], "failed to release runtime delivery claim %s",
                    error=row.get("last_error") or "send_path_degraded",
                ))
                continue
            # Each shielded row includes the Bot API call and its ledger settlement.
            # Once cancelled, do not start another row, and never refund this one.
            delivered = await settlement.wait(self._redeliver_claimed_obligations(
                [row], runtime_settlement=settlement,
            ))
            redelivered += delivered
        if not settlement.cancelled:
            with _log_suppressed(logging.DEBUG, "arming flood redelivery timers failed", exc_info=True):
                await settlement.wait(self._arm_flood_timers_for_waiting_rows())
        if settlement.cancelled:
            raise asyncio.CancelledError
        return redelivered
