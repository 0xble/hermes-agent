"""Durable async-delegation claim transitions for the gateway notification path.

Every transition opens, commits and closes a ``state.db`` connection. On a large WAL database the
commit/close can checkpoint for tens of seconds, so gateway async code must reach these only
through ``settle_durable_claims``, which runs them in a worker thread instead of on the loop.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from typing import Callable, Iterable, TypeVar

from agent.memory_provider import spawn_context_thread

logger = logging.getLogger("gateway.run")
_T = TypeVar("_T")


class _PartialClaimsError(RuntimeError):
    """A batch claim failed after taking some claims; ``claimed`` is what ``claim_off_loop`` refunds."""

    def __init__(self, cause: BaseException, claimed: list) -> None:
        super().__init__(str(cause))
        self.claimed = claimed
        self.__cause__ = cause

# kind -> (tools.async_delegation function, failure log).
_DURABLE_CLAIM_OPS = {
    "drop": ("drop_completion_delivery", "Could not drop durable completion claim"),
    "release": ("release_completion_delivery", "Could not release durable completion claim"),
    "defer": ("defer_completion_delivery", "Could not defer unadmitted completion claim"),
    "complete": ("complete_completion_delivery", "Could not acknowledge durable completion claim"),
}


def settle_durable_claim(kind: str, delegation_id: str, claim_id: str) -> bool:
    """Report whether one durable transition succeeded. Blocking: never call on the loop."""
    fn_name, fail_msg = _DURABLE_CLAIM_OPS[kind]
    try:
        import tools.async_delegation as _ad
        return getattr(_ad, fn_name)(delegation_id, claim_id)
    except Exception:
        logger.log(logging.WARNING if kind == "complete" else logging.DEBUG, fail_msg, exc_info=True)
        return False


async def settle_durable_claims(
    operations: Iterable[tuple[str, str, str]],
    settle: Callable[[str, str, str], bool | None] = settle_durable_claim,
    *,
    on_settled: Callable[[], None] | None = None,
) -> bool:
    """Settle ``(kind, delegation_id, claim_id)`` claims in ONE worker-thread hop.

    The copied context ensures the caller's profile scope still selects the ledger. One hop keeps a
    batch whole: the worker future is shielded so cancellation of the awaiting task cannot cancel a
    queued settle before the thread starts, and the thread finishes every settle regardless.
    Operations without a claim id are skipped. False reports an unsettled transition; legacy
    custom settlers returning None retain their successful no-result contract.

    ``on_settled`` runs exactly once, after every settle has actually finished (or raised): in the
    worker thread itself, so neither cancellation of the awaiting task nor loop teardown can run it
    early or skip it. It runs inline when nothing needs settling. If the default executor is gone,
    the batch settles on a dedicated thread and the callback follows it there; when no worker can
    start at all nothing settled, so the callback does not run and the error propagates.
    """
    pending = [op for op in operations if op[2]]

    def _finish() -> None:
        if on_settled is not None:
            try:
                on_settled()
            except Exception:
                logger.warning("Durable claim settle callback failed", exc_info=True)

    if not pending:
        _finish()
        return True

    def _settle_all() -> bool:
        settled = True
        try:
            for kind, delegation_id, claim_id in pending:
                if settle(kind, delegation_id, claim_id) is False:
                    settled = False
        finally:
            _finish()
        return settled

    loop = asyncio.get_running_loop()
    settle_ctx = contextvars.copy_context()
    try:
        future = loop.run_in_executor(None, settle_ctx.run, _settle_all)
    except RuntimeError:
        # The default executor is gone (loop teardown). Settle on a dedicated non-daemon thread so
        # the claims are still durably settled; ``on_settled`` runs there only after they are. If
        # even that thread cannot start, nothing settled, so the callback must not run either.
        spawn_context_thread(
            settle_ctx.run, args=(_settle_all,), name="completion-claim-settle", daemon=False,
        ).start()
        return True  # Handed off whole; a failed transition still logs from the dedicated thread.
    return await asyncio.shield(future)


async def claim_off_loop(claim: Callable[[], _T], release: Callable[[_T], None]) -> _T:
    """Run blocking ``claim()`` in a worker thread and return its result.

    The thread cannot be cancelled. If the awaiting task is cancelled first, the claim can still
    succeed with nobody left to record it, stranding its lease until expiry. Refund abandoned
    results off-loop in the copied context, even after the loop or default executor shuts down.
    ``CancelledError`` still propagates to the caller.
    """
    loop = asyncio.get_running_loop()
    claim_ctx = contextvars.copy_context()
    release_ctx = claim_ctx.copy()
    lock = threading.Lock()
    abandoned = False
    handed_over: list[_T] = []

    def _release_abandoned(result: _T) -> None:
        try:
            release_ctx.run(release, result)
        except Exception:
            logger.warning("Could not refund abandoned durable claim", exc_info=True)

    def _claim_and_handoff() -> _T:
        try:
            result = claim()  # A failed claim has nothing to release...
        except _PartialClaimsError as exc:
            # ...unless it failed partway through a batch: refund the claims already taken, here in
            # the worker, so neither the caller's cancellation nor loop teardown can strand them.
            if exc.claimed:
                _release_abandoned(exc.claimed)
            raise
        with lock:
            if not abandoned:
                handed_over.append(result)
                return result
        _release_abandoned(result)  # No loop callback or executor submission during teardown.
        return result

    future = loop.run_in_executor(None, claim_ctx.run, _claim_and_handoff)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        with lock:
            abandoned = True
        if handed_over:
            # The worker finished before cancellation, but its result was never consumed.
            threading.Thread(
                target=_release_abandoned, args=(handed_over[0],), daemon=False,
            ).start()
        raise


async def claim_siblings_off_loop(entries, consumer: str) -> list:
    """Claim each ``(evt, text)`` sibling in one worker-thread hop -> ``[(evt, text, claim_id|None)]``.
    If the awaiting task is cancelled mid-claim, every claim taken is deferred (no attempt spent)."""
    from tools.async_delegation import claim_event_delivery, defer_completion_delivery

    def _claim() -> list:
        claimed = []
        try:
            for evt, text in entries:
                claimed.append((evt, text, claim_event_delivery(evt, consumer)))
            return claimed
        except BaseException as exc:
            # Let claim_off_loop refund every claim taken before the later claim failed.
            raise _PartialClaimsError(exc, claimed) from exc

    def _defer(claimed) -> None:
        for evt, _text, claim_id in claimed:
            if claim_id:
                defer_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)

    return await claim_off_loop(_claim, _defer)
