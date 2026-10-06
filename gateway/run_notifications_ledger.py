"""Durable async-delegation claim transitions for the gateway notification path.

Every transition opens, commits and closes a ``state.db`` connection. On a large WAL database the
commit/close can checkpoint for tens of seconds, so gateway async code must reach these only
through ``settle_durable_claims``, which runs them in a worker thread instead of on the loop.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Callable, Iterable, TypeVar

logger = logging.getLogger("gateway.run")
_T = TypeVar("_T")

# kind -> (tools.async_delegation function, failure log).
_DURABLE_CLAIM_OPS = {
    "drop": ("drop_completion_delivery", "Could not drop durable completion claim"),
    "release": ("release_completion_delivery", "Could not release durable completion claim"),
    "defer": ("defer_completion_delivery", "Could not defer unadmitted completion claim"),
    "complete": ("complete_completion_delivery", "Could not acknowledge durable completion claim"),
}


def settle_durable_claim(kind: str, delegation_id: str, claim_id: str) -> None:
    """Best-effort transition of one durable completion claim. Blocking: never call on the loop."""
    fn_name, fail_msg = _DURABLE_CLAIM_OPS[kind]
    try:
        import tools.async_delegation as _ad
        getattr(_ad, fn_name)(delegation_id, claim_id)
    except Exception:
        logger.log(logging.WARNING if kind == "complete" else logging.DEBUG, fail_msg, exc_info=True)


async def settle_durable_claims(
    operations: Iterable[tuple[str, str, str]],
    settle: Callable[[str, str, str], None] = settle_durable_claim,
) -> None:
    """Settle ``(kind, delegation_id, claim_id)`` claims in ONE worker-thread hop.

    ``to_thread`` copies the context, so the caller's profile scope still selects the ledger. One
    hop keeps a batch whole: cancelling the awaiting task cannot strand siblings mid-loop, because
    the thread finishes every settle regardless. Operations without a claim id are skipped.
    """
    pending = [op for op in operations if op[2]]
    if not pending:
        return

    def _settle_all() -> None:
        for kind, delegation_id, claim_id in pending:
            settle(kind, delegation_id, claim_id)

    await asyncio.to_thread(_settle_all)


async def claim_off_loop(claim: Callable[[], _T], release: Callable[[_T], None]) -> _T:
    """Run blocking ``claim()`` in a worker thread and return its result.

    The thread cannot be cancelled. If the awaiting task is cancelled first, the claim can still
    succeed with nobody left to record it, stranding its lease until expiry. So on cancellation,
    ``release`` runs (in a worker thread, same context) on whatever the claim returns, then the
    ``CancelledError`` propagates.
    """
    loop = asyncio.get_running_loop()
    claim_ctx = contextvars.copy_context()
    release_ctx = claim_ctx.copy()
    future = loop.run_in_executor(None, claim_ctx.run, claim)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        def _release_abandoned(done: asyncio.Future) -> None:
            if done.cancelled() or done.exception() is not None:
                return
            try:
                loop.run_in_executor(None, release_ctx.run, release, done.result())
            except RuntimeError:  # executor already shut down; the lease expires on its own
                logger.warning("Could not release an abandoned durable claim", exc_info=True)

        future.add_done_callback(_release_abandoned)
        raise


async def claim_siblings_off_loop(entries, consumer: str) -> list:
    """Claim each ``(evt, text)`` sibling in one worker-thread hop -> ``[(evt, text, claim_id|None)]``.
    If the awaiting task is cancelled mid-claim, every claim taken is deferred (no attempt spent)."""
    from tools.async_delegation import claim_event_delivery, defer_completion_delivery

    def _claim() -> list:
        return [(evt, text, claim_event_delivery(evt, consumer)) for evt, text in entries]

    def _defer(claimed) -> None:
        for evt, _text, claim_id in claimed:
            if claim_id:
                defer_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)

    return await claim_off_loop(_claim, _defer)
