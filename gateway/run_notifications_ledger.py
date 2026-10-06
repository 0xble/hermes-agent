"""Durable async-delegation claim transitions for the gateway notification path.

Every transition opens, commits and closes a ``state.db`` connection. On a large WAL database the
commit/close can checkpoint for tens of seconds, so gateway async code must reach these only
through ``settle_durable_claims``, which runs them in a worker thread instead of on the loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Iterable

logger = logging.getLogger("gateway.run")

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
